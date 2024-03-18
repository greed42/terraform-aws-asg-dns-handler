from typing import Collection, NamedTuple, Optional, Set, Tuple

import json
import logging
import boto3
import botocore
import sys
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)

autoscaling = boto3.client("autoscaling")
ec2 = boto3.client("ec2")
route53 = boto3.client("route53")

HOSTNAME_TAG_NAME = "asg:hostname"
TERMINATING_TAG_NAME = "asg:terminating"
TERMINATING_TAG_VALUE = "true"
MODE_TAG_NAME = "asg:hostname_mode"
MODE_SINGLE = "single"
MODE_MULTI = "multi"
MODE_DEFAULT = MODE_MULTI
MODES_VALID = frozenset([MODE_SINGLE, MODE_MULTI])

LIFECYCLE_KEY = "LifecycleHookName"
ASG_KEY = "AutoScalingGroupName"

USE_PUBLIC_IP = os.getenv("USE_PUBLIC_IP") == "true"
ROUTE53_TTL = int(os.getenv("ROUTE53_TTL", "60"))
IPV4_FIELD = "PublicIpAddress" if USE_PUBLIC_IP else "PrivateIpAddress"
IPV6_FIELD = "Ipv6Address"


class HostnameConfig(NamedTuple):
    asg_name: str
    hostname: str
    zone_id: str
    mode: str

    @property
    def is_single(self) -> bool:
        return self.mode == MODE_SINGLE

    @property
    def canonical_hostname(self) -> str:
        return f'{self.hostname.rstrip(".")}.'

    @classmethod
    def from_asg_name(cls, asg_name: str) -> Optional["HostnameConfig"]:
        logger.info("Fetching tags for ASG: %s", asg_name)

        tags = {}

        for response in autoscaling.get_paginator("describe_tags").paginate(
            Filters=[
                {"Name": "auto-scaling-group", "Values": [asg_name]},
                {"Name": "key", "Values": [HOSTNAME_TAG_NAME, MODE_TAG_NAME]},
            ],
        ):
            for tag in response.get("Tags", []):
                key, value = tag["Key"], tag["Value"]
                logger.info("Found tag for ASG %s: %s = %s", asg_name, key, value)
                tags[key] = value

        # We MUST have a hostname tag, we MAY have a mode.
        hostname_zone = tags.get(HOSTNAME_TAG_NAME, "").split("@", 1)

        if not hostname_zone:
            logger.warning("Cannot find %s tag for ASG %s", HOSTNAME_TAG_NAME, asg_name)
            return None

        if len(hostname_zone) != 2:
            logger.error(
                "Tag %s for ASG %s value %s does not contain @<ZONE_ID>.",
                HOSTNAME_TAG_NAME,
                asg_name,
                hostname_zone,
            )
            return None

        mode = tags.get(MODE_TAG_NAME, MODE_DEFAULT)
        if mode not in MODES_VALID:
            logger.error(
                "Tag %s for ASG %s value %s is an unknown mode.",
                MODE_TAG_NAME,
                asg_name,
                mode,
            )
            return None

        hostname, zone = hostname_zone

        return cls(asg_name, hostname, zone, mode)


def fetch_instance_ips(
    asg_name: str, ignore_ids: Collection[str]
) -> Tuple[Set[str], Set[str]]:
    ipv4s = set()
    ipv6s = set()

    for response in ec2.get_paginator("describe_instances").paginate(
        Filters=[
            {
                "Name": "tag:aws:autoscaling:groupName",
                "Values": [asg_name],
            },
            {
                "Name": "instance-state-name",
                "Values": ["running"],
            },
        ]
    ):
        for reservation in response["Reservations"]:
            for instance in reservation["Instances"]:
                if instance["InstanceId"] in ignore_ids:
                    logging.info("Ignoring instance-id %s", instance["InstanceId"])
                    continue

                if any(
                    tag["Key"] == TERMINATING_TAG_NAME
                    and tag["Value"] == TERMINATING_TAG_VALUE
                    for tag in instance["Tags"]
                ):
                    logging.info(
                        "Instance-id %s is tagged for termination",
                        instance["InstanceId"],
                    )
                    continue

                logger.info("Fetching IP for instance-id: %s", instance["InstanceId"])

                if (ipv4 := instance.get(IPV4_FIELD)) is not None:
                    logging.info(
                        "Found instance-id %s IPv4 %s", instance["InstanceId"], ipv4
                    )
                    ipv4s.add(ipv4)

                if (ipv6 := instance.get(IPV6_FIELD)) is not None:
                    logging.info(
                        "Found instance-id %s IPv6 %s", instance["InstanceId"], ipv6
                    )
                    ipv6s.add(ipv6)

    return ipv4s, ipv6s


def fetch_ip_from_ec2(instance_id: str) -> Tuple[Set[str], Set[str]]:
    ipv4s = set()
    ipv6s = set()

    for response in ec2.get_paginator("describe_instances").paginate(
        InstanceIds=[instance_id]
    ):
        for reservation in response["Reservations"]:
            for instance in reservation["Instances"]:
                logger.info("Fetching IP for instance-id: %s", instance["InstanceId"])

                if (ipv4 := instance.get(IPV4_FIELD)) is not None:
                    logging.info(
                        "Found instance-id %s IPv4 %s", instance["InstanceId"], ipv4
                    )
                    ipv4s.add(ipv4)

                if (ipv6 := instance.get(IPV6_FIELD)) is not None:
                    logging.info(
                        "Found instance-id %s IPv6 %s", instance["InstanceId"], ipv6
                    )
                    ipv6s.add(ipv6)

    return ipv4s, ipv6s


def set_terminating_tag(instance_id):
    logger.info(
        "Adding %s = %s tag for instance-id %s",
        TERMINATING_TAG_NAME,
        TERMINATING_TAG_VALUE,
        instance_id,
    )
    ec2.create_tags(
        Resources=[instance_id],
        Tags=[
            {
                "Key": TERMINATING_TAG_NAME,
                "Value": TERMINATING_TAG_VALUE,
            }
        ],
    )


# Fetches values of an RR from route53 API
def fetch_values_from_route53(
    config: HostnameConfig, record_type: str = "A"
) -> Set[str]:
    logger.info("Fetching IP for hostname: %s", config.hostname)

    values = set()

    response = route53.list_resource_record_sets(
        HostedZoneId=config.zone_id,
        StartRecordName=config.hostname,
        StartRecordType=record_type,
        # We want only the set with the given name and type.
        # So we set max items = 1 and ignore pagination.
        MaxItems="1",
    )

    for rr_set in response["ResourceRecordSets"]:
        # If there is _no_ record with the desired name but there is basically
        # anything else in the zone we will probably get some other record.
        # But that's how Route53 works via the API, so skip over anything that
        # doesn't match our query.
        if rr_set["Name"] != config.canonical_hostname:
            logging.debug(
                "Undesired RR set name: %s (wanted %s)",
                rr_set["Name"],
                config.canonical_hostname,
            )
            continue
        if rr_set["Type"] != record_type:
            logging.debug(
                "Undesired RR set type: %s (wanted %s)", rr_set["Type"], record_type
            )
            continue

        for rr in rr_set["ResourceRecords"]:
            values.add(rr["Value"])

    if values:
        logger.info(
            "Found %s record values for hostname %s: %s",
            record_type,
            config.hostname,
            values,
        )
    else:
        logger.info(
            "No %s record values for hostname %s",
            record_type,
            config.hostname,
        )

    return values


# Updates a Route53 record
def update_record(
    config: HostnameConfig,
    values: Collection[str],
    operation: str,
    record_type: str = "A",
):
    logger.info(
        "Changing record with %s for %s -> %s in %s",
        operation,
        config.hostname,
        values,
        config.zone_id,
    )
    route53.change_resource_record_sets(
        HostedZoneId=config.zone_id,
        ChangeBatch={
            "Changes": [
                {
                    "Action": operation,
                    "ResourceRecordSet": {
                        "Name": config.hostname,
                        "Type": record_type,
                        "TTL": ROUTE53_TTL,
                        "ResourceRecords": [{"Value": value} for value in values],
                    },
                }
            ]
        },
    )


# Processes a scaling event
# Builds a hostname from tag metadata, fetches a IP, and updates records accordingly
def process_message(message):
    if "LifecycleTransition" not in message:
        logger.info("Skipping %s event", message["Event"])
        return
    logger.info("Processing %s lifecycle transition", message["LifecycleTransition"])

    launch_ids = set()
    terminate_ids = set()

    if message["LifecycleTransition"] == "autoscaling:EC2_INSTANCE_LAUNCHING":
        launch_ids = {message["EC2InstanceId"]}
    elif (
        message["LifecycleTransition"] == "autoscaling:EC2_INSTANCE_TERMINATING"
        or message["LifecycleTransition"] == "autoscaling:EC2_INSTANCE_LAUNCH_ERROR"
    ):
        terminate_ids = {message["EC2InstanceId"]}
    else:
        logger.error(
            "Encountered unknown event type: %s", message["LifecycleTransition"]
        )
        return

    logger.info("Processing message: %s", message)

    # Tag the instance so it isn't seen by later invocations of
    # this function. Explicitly ignore it now so we don't have to
    # worry about consistency races.
    for instance in terminate_ids:
        set_terminating_tag(instance)

    asg_name = message["AutoScalingGroupName"]

    config = HostnameConfig.from_asg_name(asg_name)

    if config is None:
        return

    def change_records(old: Set[str], new: Set[str], record_type: str = "A"):
        if old == new:
            logging.info(
                "No change in %s %s records %s", config.hostname, record_type, new
            )
            return

        if new:
            if add := new - old:
                logging.info(
                    "Adding %s %s records %s", config.hostname, record_type, add
                )
            if remove := old - new:
                logging.info(
                    "Removing %s %s records %s", config.hostname, record_type, remove
                )

            update_record(config, new, "UPSERT", record_type)
            return

        logging.info("Removing all %s %s records %s", config.hostname, record_type, old)
        update_record(config, old, "DELETE", record_type)

    old_ipv4s = fetch_values_from_route53(config)
    old_ipv6s = fetch_values_from_route53(config, "AAAA")

    if config.is_single:
        # Single mode: The most recent started instance gets the hostname.
        # This is intended for replicas = 1 autoscaling groups that can
        # be updated with launch_before_terminate so reduce the visible
        # downtime during maintenance.
        for instance_id in launch_ids:
            ipv4s, ipv6s = fetch_ip_from_ec2(instance_id)
            # In this mode, we cannot tell if we went from a "has IPv6" to "no IPv6"
            # situation: we rely on the terminating event to delete the address properly.
            # (Same for IPv4 really.)
            for old, new, record_type in (
                (old_ipv4s, ipv4s, "A"),
                (old_ipv6s, ipv6s, "AAAA"),
            ):
                if new:
                    change_records(old, new, record_type)

        # We rely on the Route53 API to only allow deletion of the listed
        # addresses. So, in the case of a new instance starting and "taking
        # over" the hostname before this one termintes, the name will remain
        # pointing at that instance.
        for instance_id in terminate_ids:
            ipv4s, ipv6s = fetch_ip_from_ec2(instance_id)
            for old, record_type in ((ipv4s, "A"), (ipv6s, "AAAA")):
                if old:
                    try:
                        change_records(old, set(), record_type)
                    except botocore.exceptions.ClientError as error:
                        if (
                            error.response.get("Error", {}).get("Code")
                            != "InvalidChangeBatch"
                        ):
                            raise
                        logging.info(
                            "%s %s records have already been changed",
                            config.hostname,
                            record_type,
                        )

        return

    # Multi mode: The hostname references all running instances.

    ipv4s, ipv6s = fetch_instance_ips(asg_name, terminate_ids)

    change_records(old_ipv4s, ipv4s)
    change_records(old_ipv6s, ipv6s, "AAAA")


# Picks out the message from a SNS message and deserializes it
def process_record(record):
    process_message(json.loads(record["Sns"]["Message"]))


# Main handler where the SNS events end up to
# Events are bulked up, so process each Record individually
def lambda_handler(event, context):
    del context  # unused
    logger.info("Processing SNS event: %s", json.dumps(event))

    for record in event["Records"]:
        process_record(record)

        message = json.loads(record["Sns"]["Message"])
        if LIFECYCLE_KEY in message and ASG_KEY in message:
            # Finish the asg lifecycle operation by sending a continue result
            logger.info("Finishing ASG action")
            try:
                response = autoscaling.complete_lifecycle_action(
                    LifecycleHookName=message["LifecycleHookName"],
                    AutoScalingGroupName=message["AutoScalingGroupName"],
                    InstanceId=message["EC2InstanceId"],
                    LifecycleActionToken=message["LifecycleActionToken"],
                    LifecycleActionResult="CONTINUE",
                )
                logger.info("ASG action complete: %s", response)
            except botocore.exceptions.ClientError as error:
                logger.info("Ignoring complete lifecycle action error: %s", error)

        else:
            logger.error("No valid JSON message")


# if invoked manually, assume someone pipes in a event json
if __name__ == "__main__":
    logging.basicConfig()

    lambda_handler(json.load(sys.stdin), None)
