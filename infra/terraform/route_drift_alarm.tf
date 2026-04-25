# ── Detection: alert if someone manually deletes the bronze-CDC routes ──
#
# `prevent_destroy` in vpc_peering.tf only blocks Terraform from deleting
# these resources. It does NOT stop someone from removing them via the
# AWS console / CLI / SDK — which is exactly how the 2026-04-25 incident
# happened (3rd time). This file wires CloudTrail → EventBridge →
# CloudWatch Logs so any DeleteRoute / DeleteVpcPeeringConnection call
# against the critical resources is captured as an alarm-able event.
#
# IMPORTANT: EventBridge does NOT receive CloudTrail Management Events
# unless an actual `aws_cloudtrail` Trail exists in the account. The bare
# "CloudTrail Event History" (the default 90-day console view) does NOT
# forward to EventBridge. The trail below is what makes the rule fire.

# ── CloudTrail trail (required for EventBridge to receive API events) ──

resource "aws_s3_bucket" "cloudtrail" {
  count         = var.vpc_peering_enabled ? 1 : 0
  bucket        = "${var.prefix}-cloudtrail"
  force_destroy = true
  tags          = { Name = "${var.prefix}-cloudtrail" }
}

resource "aws_s3_bucket_policy" "cloudtrail" {
  count  = var.vpc_peering_enabled ? 1 : 0
  bucket = aws_s3_bucket.cloudtrail[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AWSCloudTrailAclCheck"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:GetBucketAcl"
        Resource  = aws_s3_bucket.cloudtrail[0].arn
      },
      {
        Sid       = "AWSCloudTrailWrite"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.cloudtrail[0].arn}/AWSLogs/${data.aws_caller_identity.current.account_id}/*"
        Condition = {
          StringEquals = { "s3:x-amz-acl" = "bucket-owner-full-control" }
        }
      },
    ]
  })
}

resource "aws_cloudtrail" "main" {
  count                         = var.vpc_peering_enabled ? 1 : 0
  name                          = "${var.prefix}-management-events"
  s3_bucket_name                = aws_s3_bucket.cloudtrail[0].id
  is_multi_region_trail         = true
  include_global_service_events = true
  enable_logging                = true
  depends_on                    = [aws_s3_bucket_policy.cloudtrail]
  tags                          = { Name = "${var.prefix}-management-events" }
}

resource "aws_cloudwatch_log_group" "route_drift" {
  count             = var.vpc_peering_enabled ? 1 : 0
  name              = "/aws/events/${var.prefix}-route-drift"
  retention_in_days = 90
  tags              = { Name = "${var.prefix}-route-drift" }
}

# CloudWatch Logs resource policy so EventBridge can write to the group.
resource "aws_cloudwatch_log_resource_policy" "route_drift_events" {
  count           = var.vpc_peering_enabled ? 1 : 0
  policy_name     = "${var.prefix}-route-drift-events"
  policy_document = data.aws_iam_policy_document.route_drift_events[0].json
}

data "aws_iam_policy_document" "route_drift_events" {
  count = var.vpc_peering_enabled ? 1 : 0
  statement {
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com", "delivery.logs.amazonaws.com"]
    }
    resources = [
      "${aws_cloudwatch_log_group.route_drift[0].arn}:*",
    ]
  }
}

# Catch any DeleteRoute targeting the MSK private route table OR any
# Delete/AcceptVpcPeering targeting the bronze peering connection.
resource "aws_cloudwatch_event_rule" "route_drift" {
  count       = var.vpc_peering_enabled ? 1 : 0
  name        = "${var.prefix}-route-drift"
  description = "Bronze CDC critical-route deletion / peering teardown"

  event_pattern = jsonencode({
    source      = ["aws.ec2"]
    detail-type = ["AWS API Call via CloudTrail"]
    detail = {
      eventSource = ["ec2.amazonaws.com"]
      eventName = [
        "DeleteRoute",
        "ReplaceRoute",
        "DeleteVpcPeeringConnection",
        "ModifyVpcPeeringConnectionOptions",
      ]
      requestParameters = {
        # Match either the MSK private route table OR the peering id.
        # CloudTrail emits `routeTableId` for DeleteRoute/ReplaceRoute and
        # `vpcPeeringConnectionId` for the peering events.
        routeTableId = [
          aws_route_table.private.id,
        ]
      }
    }
  })

  tags = { Name = "${var.prefix}-route-drift" }
}

# Same rule but matching peering-targeted events (pattern requires a
# separate rule because EventBridge OR-matches across keys not within).
resource "aws_cloudwatch_event_rule" "peering_drift" {
  count       = var.vpc_peering_enabled ? 1 : 0
  name        = "${var.prefix}-peering-drift"
  description = "Bronze CDC peering teardown / option flip"

  event_pattern = jsonencode({
    source      = ["aws.ec2"]
    detail-type = ["AWS API Call via CloudTrail"]
    detail = {
      eventSource = ["ec2.amazonaws.com"]
      eventName = [
        "DeleteVpcPeeringConnection",
        "RejectVpcPeeringConnection",
        "ModifyVpcPeeringConnectionOptions",
      ]
      requestParameters = {
        vpcPeeringConnectionId = [
          aws_vpc_peering_connection.databricks_msk[0].id,
        ]
      }
    }
  })

  tags = { Name = "${var.prefix}-peering-drift" }
}

resource "aws_cloudwatch_event_target" "route_drift_to_logs" {
  count     = var.vpc_peering_enabled ? 1 : 0
  rule      = aws_cloudwatch_event_rule.route_drift[0].name
  target_id = "${var.prefix}-route-drift-logs"
  arn       = aws_cloudwatch_log_group.route_drift[0].arn
}

resource "aws_cloudwatch_event_target" "peering_drift_to_logs" {
  count     = var.vpc_peering_enabled ? 1 : 0
  rule      = aws_cloudwatch_event_rule.peering_drift[0].name
  target_id = "${var.prefix}-peering-drift-logs"
  arn       = aws_cloudwatch_log_group.route_drift[0].arn
}

# Metric filter: every captured event becomes a count, exposed as a
# CloudWatch metric so we can alarm on it (and later page via SNS).
resource "aws_cloudwatch_log_metric_filter" "route_drift_count" {
  count          = var.vpc_peering_enabled ? 1 : 0
  name           = "${var.prefix}-route-drift-count"
  log_group_name = aws_cloudwatch_log_group.route_drift[0].name
  pattern        = "{ $.detail.eventName = * }"

  metric_transformation {
    name      = "BronzeRouteDriftEvents"
    namespace = "Nexus/Infra"
    value     = "1"
    unit      = "Count"
  }
}

resource "aws_cloudwatch_metric_alarm" "route_drift_alarm" {
  count               = var.vpc_peering_enabled ? 1 : 0
  alarm_name          = "${var.prefix}-bronze-route-drift"
  alarm_description   = "Someone deleted/replaced a bronze-CDC route or peering. Restore via `terraform apply` against vpc_peering.tf."
  namespace           = "Nexus/Infra"
  metric_name         = "BronzeRouteDriftEvents"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  # No SNS action wired by default — the alarm appears in the console and
  # can be tied to a topic later. Add `alarm_actions = [<sns_arn>]` to page.
}
