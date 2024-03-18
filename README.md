# ASG DNS handler | [![Build Status](https://cloud.drone.io/api/badges/meltwater/terraform-aws-asg-dns-handler/status.svg)](https://cloud.drone.io/meltwater/terraform-aws-asg-dns-handler)

This Terraform module sets up everything necessary for dynamically setting hostnames following a certain pattern on instances spawned by AWS Auto Scaling Groups (ASGs).

This fork uses a different approach, and offers two modes of operation:

1.  All IPs for instances in an auto-scaling group are added as `A`
    (and `AAAA`) records for a single hostname associated with that
    autoscaling group.

    This is suitable for clients which can fail over using DNS, and
    for whatever reason you do not want to set up a loadbalancer:
    either the protocols are unsuitable or the cost of the
    loadbalancer is overly high for the work the service needs to
    perform.

1.  The most recent instance in an auto-scaling group becomes the sole
    `A` (and `AAAA`) record for that single hostname.

    This is intended for a service which otherwise can run as a
    singleton, but for which you would like "launch before terminate"
    update semantics to reduce planned downtime.


The consideration in the upstream blog post, [Dynamic Route53 records
for AWS Auto Scaling Groups with
Terraform](https://underthehood.meltwater.com/blog/2020/02/07/dynamic-route53-records-for-aws-auto-scaling-groups-with-terraform/),
also applies. I am grateful for their work on this module and thank
them for licensing it to the community.

## Maintainers

This repository and the module it houses is maintained by myself,
Graham Reed, to solve a few particular problems which do not warrant
load balancers. You are welcome to use it, and I will consider PRs,
but I cannot offer actual support.

## Requirements

- [Terraform](https://www.terraform.io/downloads.html) 1.6+
- [Terraform AWS provider](https://github.com/terraform-providers/terraform-provider-aws) 5.0+

## How do I use it?

Create an ASG and set the `asg:hostname` tag for example like this:

```
cant-use-a-loadbalancer.internal-vpc.testing@Z3QP9GZSRL8IVA
```

Optionally, include the tag `asg:hostname_mode` tag with the value `multi` or
`single`; `multi` is assumed if the tag is not present. `multi` mode
will add all IPs to the hostname, `single` uses only the most recent
instance (as determined by SNS messages to the lambda) to get the IP.

```hcl
data "aws_route53_zone" "internal" {
  name         = "internal-vpc.testing"
  private_zone = true
}

resource "autoscaling_group" "test" {
  ...

  tag {
    key                 = "asg:hostname_pattern"
    value               = "cant-user-a-loadbalancer.${data.aws_route53_zone.internal.name}@${data.aws_route53_zone.internal.id}"
    propagate_at_launch = false
  }
  tag {
    key                 = "asg:hostname_mode"
    value               = "multi"
    propagate_at_launch = false
  }
}
```

Only the tags on the autoscaling group are read.

Once you have your ASG set up, you can just invoke this module and point to it:

```hcl
module "clever_name_autoscale_dns" {
  source  = meltwater/asg-dns-handler/aws"
  version = "~> 2.0"

  use_public_ip                       = false
  route53_record_ttl                  = 60
  autoscale_handler_unique_identifier = "clever_name"
  autoscale_route53zone_arn           = data.aws_route53_zone.internal.id
  vpc_name                            = "my_vpc"
}
```

## How does it work?

The module sets up these things:

1. A SNS topic
2. A Lambda function
3. A topic subscription sending SNS events to the Lambda function

The Lambda function then does the following:

- Fetch the `asg:hostname` tag value from the ASG, and parse out the hostname and Route53 zone ID from it.
- Fetch the `asg:hostname_mode` tag value, or assume `multi` if it is not present.

- If an instance is being **deleted**, the tag `asg:terminating` is
  added with the value `true`. This is used so subsequent runs in
  `multi` mode know to skip the instance. (But it's added regardless
  of the mode.)

- In `single` mode:
  - Obtain the IP from the instance
  - If it's an instance being **created**
    - Create or replace a Route53 record pointing the hostname to the IP (UPSERT)
  - If it's an instance being **deleted**
    - Delete the Route53 record pointing to the IP (DELETE)
    - This will (per the Route53 API) fail if the record is pointing to
      a different IP, and that is intended.

- In `multi` mode:
  - Find the IP of all instances in the ASG except for those known to
    be terminating (Those for which we receive a notification saying
    so, or which already have an `asg:terminating=true` tag.)
  - Find any current IPs of the hostname from Route53.
  - If there are any changes...
    - If there are current IPs, create or update the record
      with the new addresses (UPSERT)
    - If there are no current IPs (the last instance was terminated),
      remove the record (DELETE)

## Setup

Add `initial_lifecycle_hook` definitions to your `aws_autoscaling_group` resource , like so:

```hcl
resource "aws_autoscaling_group" "my_asg" {
  name = "myASG"

  vpc_zone_identifier = var.aws_subnets

  min_size                  = var.asg_min_count
  max_size                  = var.asg_max_count
  desired_capacity          = var.asg_desired_count
  health_check_type         = "EC2"
  health_check_grace_period = 300
  force_delete              = false

  launch_configuration = aws_launch_configuration.my_launch_config.name

  lifecycle {
    create_before_destroy = true
  }

  initial_lifecycle_hook {
    name                    = "lifecycle-launching"
    default_result          = "ABANDON"
    heartbeat_timeout       = 60
    lifecycle_transition    = "autoscaling:EC2_INSTANCE_LAUNCHING"
    notification_target_arn = module.autoscale_dns.autoscale_handling_sns_topic_arn
    role_arn                = module.autoscale_dns.agent_lifecycle_iam_role_arn
  }

  initial_lifecycle_hook {
    name                    = "lifecycle-terminating"
    default_result          = "ABANDON"
    heartbeat_timeout       = 60
    lifecycle_transition    = "autoscaling:EC2_INSTANCE_TERMINATING"
    notification_target_arn = module.autoscale_dns.autoscale_handling_sns_topic_arn
    role_arn                = module.autoscale_dns.agent_lifecycle_iam_role_arn
  }

  tag {
    key                 = "asg:hostname_pattern"
    value               = "${var.hostname_prefix}-#instanceid.${var.vpc_name}.testing@${var.internal_zone_id}"
    propagate_at_launch = true
  }
}

module "autoscale_dns" {
  source  = "meltwater/asg-dns-handler/aws"
  version = "2.1.7"

  autoscale_handler_unique_identifier = "my_asg_handler"
  autoscale_route53zone_arn           = var.internal_zone_id
  vpc_name                            = var.vpc_name
}
```

## Developers Guide / Contributing

Please read [CONTRIBUTING.md](CONTRIBUTING.md) to understand how to submit pull requests to us, and also see our [Code of Conduct](CODE_OF_CONDUCT.md).

## Difference between Lifecycle action

Lifecycle_hook can have `CONTINUE` or `ABANDON` as default_result. By setting default_result to `ABANDON` will terminate the instance if the lambda function fails to update the DNS record as required. `Complete_lifecycle_action` in lambda function returns `LifecycleActionResult` as `CONTINUE` on success to Lifecycle_hook. But if lambda function fails, Lifecycle_hook doesn't get any response from `Complete_lifecycle_action` which results in timeout and terminates the instance.

At the conclusion of a lifecycle hook, the result is either ABANDON or CONTINUE.
If the instance is launching, CONTINUE indicates that your actions were successful, and that the instance can be put into service. Otherwise, ABANDON indicates that your custom actions were unsuccessful, and that the instance can be terminated.

If the instance is terminating, both ABANDON and CONTINUE allow the instance to terminate. However, ABANDON stops any remaining actions, such as other lifecycle hooks, while CONTINUE allows any other lifecycle hooks to complete.

## License and Copyright

This project was built at Meltwater. It is licensed under the [Apache License 2.0](LICENSE).
