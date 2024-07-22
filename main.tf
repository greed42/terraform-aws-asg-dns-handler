resource "aws_sns_topic" "autoscale_handling" {
  name = "${var.vpc_name}-${var.autoscale_handler_unique_identifier}"
}

resource "aws_cloudwatch_log_group" "autoscale_handling" {
  name              = "/aws/lambda/${aws_lambda_function.autoscale_handling.function_name}"
  retention_in_days = var.lambda_log_retention_in_days
}

data "aws_iam_policy_document" "autoscale_handling" {
  statement {
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.autoscale_handling.arn}:*"]
  }
  statement {
    actions = [
      "autoscaling:DescribeTags",
      "autoscaling:DescribeAutoScalingGroups",
      "autoscaling:CompleteLifecycleAction",
      "ec2:DescribeInstances",
      "route53:GetHostedZone",
    ]
    resources = ["*"]
  }
  statement {
    actions   = ["ec2:CreateTags"]
    resources = ["arn:aws:ec2:*:*:instance/*"]

    condition {
      variable = "aws:TagKeys"
      test     = "ForAllValues:StringEquals"
      values   = ["asg:terminating"]
    }

    dynamic "condition" {
      for_each = var.ec2_resource_tags

      content {
        variable = "ec2:ResourceTag/${condition.key}"
        test     = "StringEquals"
        values   = condition.value
      }
    }
  }
  statement {
    actions   = ["route53:ListResourceRecordSets"]
    resources = ["arn:aws:route53:::hostedzone/${var.autoscale_route53zone_arn}"]
  }
  statement {
    actions   = ["route53:ChangeResourceRecordSets"]
    resources = ["arn:aws:route53:::hostedzone/${var.autoscale_route53zone_arn}"]

    condition {
      variable = "route53:ChangeResourceRecordSetsRecordTypes"
      test     = "ForAllValues:StringEquals"
      values = [
        "A",
        "AAAA",
      ]
    }

    dynamic "condition" {
      for_each = toset(var.route53_change_record_names == null ? [] : ["rr_name"])

      content {
        variable = "route53:ChangeResourceRecordSetsNormalizedRecordNames"
        test     = "ForAllValues:StringLike"
        values   = var.route53_change_record_names
      }
    }
  }
}

resource "aws_iam_role_policy" "autoscale_handling" {
  name   = "${var.vpc_name}-${var.autoscale_handler_unique_identifier}"
  role   = aws_iam_role.autoscale_handling.name
  policy = data.aws_iam_policy_document.autoscale_handling.json
}

data "aws_iam_policy_document" "autoscale_handling_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "autoscale_handling" {
  name = "${var.vpc_name}-${var.autoscale_handler_unique_identifier}"

  assume_role_policy = data.aws_iam_policy_document.autoscale_handling_role.json
}

resource "aws_iam_role" "lifecycle" {
  name               = "${var.vpc_name}-${var.autoscale_handler_unique_identifier}-lifecycle"
  assume_role_policy = data.aws_iam_policy_document.lifecycle.json
}

data "aws_iam_policy_document" "lifecycle" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["autoscaling.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "lifecycle_policy" {
  name   = "${var.vpc_name}-${var.autoscale_handler_unique_identifier}-lifecycle"
  role   = aws_iam_role.lifecycle.id
  policy = data.aws_iam_policy_document.lifecycle_policy.json
}

data "aws_iam_policy_document" "lifecycle_policy" {
  statement {
    effect    = "Allow"
    actions   = ["sns:Publish", "autoscaling:CompleteLifecycleAction"]
    resources = [aws_sns_topic.autoscale_handling.arn]
  }
}

data "archive_file" "autoscale" {
  type        = "zip"
  source_file = "${path.module}/lambda/autoscale/autoscale.py"
  output_path = "${path.module}/lambda/dist/autoscale.zip"
}

resource "aws_lambda_function" "autoscale_handling" {
  depends_on = [aws_sns_topic.autoscale_handling]

  filename         = data.archive_file.autoscale.output_path
  function_name    = "${var.vpc_name}-${var.autoscale_handler_unique_identifier}"
  role             = aws_iam_role.autoscale_handling.arn
  handler          = "autoscale.lambda_handler"
  runtime          = "python${var.python_version}"
  source_code_hash = filebase64sha256(data.archive_file.autoscale.output_path)
  description      = "Handles DNS for autoscaling groups by receiving autoscaling notifications and setting/deleting records from route53"
  environment {
    variables = {
      "USE_PUBLIC_IP" = var.use_public_ip
      "ROUTE53_TTL"   = var.route53_record_ttl
    }
  }
}

resource "aws_lambda_permission" "autoscale_handling" {
  depends_on = [aws_lambda_function.autoscale_handling]

  statement_id  = "AllowExecutionFromSNS"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.autoscale_handling.arn
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.autoscale_handling.arn
}

resource "aws_sns_topic_subscription" "autoscale_handling" {
  depends_on = [aws_lambda_permission.autoscale_handling]

  topic_arn = aws_sns_topic.autoscale_handling.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.autoscale_handling.arn
}
