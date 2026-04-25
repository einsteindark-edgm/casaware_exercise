# ── Auto-healing for the bronze-CDC return route ──────────────────────
#
# `prevent_destroy` + `ignore_changes` on aws_route.msk_to_databricks_private
# protect against Terraform removing the route. They do NOT protect against:
#   - someone running `terraform apply -replace=aws_route.msk_to_databricks_private[0]`
#   - someone running `terraform destroy -target=...`
#   - someone deleting the route via AWS console / CLI / SDK / another agent
#
# The 2026-04-25 incident (5 cycles in 21 hours) happened because *something*
# ran `aws ec2 delete-route` on rtb-0b520075f20487eb1 from this account. The
# CloudTrail trail in route_drift_alarm.tf already captures every DeleteRoute
# event — this file adds a Lambda target that recreates the route within
# seconds. Defense in depth: control plane (TF lifecycle) + data plane (Lambda).
#
# The EventBridge rule `aws_cloudwatch_event_rule.route_drift` already filters
# DeleteRoute / ReplaceRoute on rtb-0b520075f20487eb1, so this Lambda only
# fires when the bronze-critical route is touched.

# ── Lambda IAM ────────────────────────────────────────────────────────

data "aws_iam_policy_document" "route_self_heal_assume" {
  count = var.vpc_peering_enabled ? 1 : 0
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "route_self_heal" {
  count              = var.vpc_peering_enabled ? 1 : 0
  name               = "${var.prefix}-route-self-heal"
  assume_role_policy = data.aws_iam_policy_document.route_self_heal_assume[0].json
  tags               = { Name = "${var.prefix}-route-self-heal" }
}

# Minimal permissions: create/describe routes + CW logs. Scoped to the
# specific route table so the Lambda cannot heal anything else.
data "aws_iam_policy_document" "route_self_heal_inline" {
  count = var.vpc_peering_enabled ? 1 : 0

  statement {
    sid     = "WriteRouteOnBronzeRtb"
    actions = ["ec2:CreateRoute", "ec2:ReplaceRoute"]
    resources = [
      "arn:aws:ec2:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:route-table/${aws_route_table.private.id}",
    ]
  }

  statement {
    sid       = "DescribeRoutes"
    actions   = ["ec2:DescribeRouteTables", "ec2:DescribeVpcPeeringConnections"]
    resources = ["*"]
  }

  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.prefix}-route-self-heal:*"]
  }
}

resource "aws_iam_role_policy" "route_self_heal" {
  count  = var.vpc_peering_enabled ? 1 : 0
  name   = "${var.prefix}-route-self-heal"
  role   = aws_iam_role.route_self_heal[0].id
  policy = data.aws_iam_policy_document.route_self_heal_inline[0].json
}

data "aws_region" "current" {}

# ── Lambda code (inline) ──────────────────────────────────────────────

locals {
  # Single source of truth for what "the bronze return route" means.
  # If you ever change the workspace CIDR or the peering, edit HERE — the
  # Lambda reads these from env vars, no code changes needed.
  route_self_heal_env = {
    ROUTE_TABLE_ID            = aws_route_table.private.id
    DESTINATION_CIDR_BLOCK    = var.workspace_vpc_cidr
    VPC_PEERING_CONNECTION_ID = var.vpc_peering_enabled ? aws_vpc_peering_connection.databricks_msk[0].id : ""
  }
}

data "archive_file" "route_self_heal" {
  count       = var.vpc_peering_enabled ? 1 : 0
  type        = "zip"
  output_path = "${path.module}/.build/route_self_heal.zip"
  source {
    filename = "index.py"
    content  = <<-PY
      """Restore the bronze-CDC return route when something deletes it.

      Triggered by EventBridge rule `${var.prefix}-route-drift`, which fires
      on DeleteRoute / ReplaceRoute targeting rtb-... (the MSK private rtb).

      Idempotent: if the route is already present we no-op. Logs every action
      so you can see WHO did the delete by correlating with the trigger event.
      """
      import json
      import logging
      import os
      import boto3
      from botocore.exceptions import ClientError

      log = logging.getLogger()
      log.setLevel(logging.INFO)
      ec2 = boto3.client("ec2")

      RTB    = os.environ["ROUTE_TABLE_ID"]
      CIDR   = os.environ["DESTINATION_CIDR_BLOCK"]
      PCX    = os.environ["VPC_PEERING_CONNECTION_ID"]


      def handler(event, context):
          log.info("trigger event: %s", json.dumps(event))
          detail = event.get("detail", {}) or {}
          rp = detail.get("requestParameters", {}) or {}

          # Only act when the offending event matches OUR route.
          if rp.get("routeTableId") != RTB:
              log.info("ignoring: rtb mismatch (%s != %s)", rp.get("routeTableId"), RTB)
              return {"status": "ignored_rtb"}
          if rp.get("destinationCidrBlock") not in (CIDR, None):
              # None = ReplaceRoute on a different cidr — only heal our cidr.
              log.info("ignoring: cidr mismatch (%s != %s)", rp.get("destinationCidrBlock"), CIDR)
              return {"status": "ignored_cidr"}

          # If a route for our CIDR already exists, no-op.
          rtb = ec2.describe_route_tables(RouteTableIds=[RTB])["RouteTables"][0]
          for r in rtb.get("Routes", []):
              if r.get("DestinationCidrBlock") == CIDR and r.get("VpcPeeringConnectionId") == PCX and r.get("State") == "active":
                  log.info("no-op: route already present and active")
                  return {"status": "noop_present"}

          actor = detail.get("userIdentity", {}).get("arn", "?")
          src   = detail.get("sourceIPAddress", "?")
          ua    = detail.get("userAgent", "?")
          log.warning(
              "RESTORING route %s -> %s on %s. Deleted by actor=%s ip=%s ua=%s",
              CIDR, PCX, RTB, actor, src, ua,
          )

          try:
              ec2.create_route(
                  RouteTableId=RTB,
                  DestinationCidrBlock=CIDR,
                  VpcPeeringConnectionId=PCX,
              )
              return {"status": "restored", "actor": actor}
          except ClientError as e:
              # If the route exists (race with another invocation), that's fine.
              if e.response.get("Error", {}).get("Code") == "RouteAlreadyExists":
                  return {"status": "raced_already_exists"}
              raise
    PY
  }
}

resource "aws_cloudwatch_log_group" "route_self_heal" {
  count             = var.vpc_peering_enabled ? 1 : 0
  name              = "/aws/lambda/${var.prefix}-route-self-heal"
  retention_in_days = 90
}

resource "aws_lambda_function" "route_self_heal" {
  count            = var.vpc_peering_enabled ? 1 : 0
  function_name    = "${var.prefix}-route-self-heal"
  role             = aws_iam_role.route_self_heal[0].arn
  handler          = "index.handler"
  runtime          = "python3.11"
  filename         = data.archive_file.route_self_heal[0].output_path
  source_code_hash = data.archive_file.route_self_heal[0].output_base64sha256
  timeout          = 30

  environment {
    variables = local.route_self_heal_env
  }

  depends_on = [
    aws_iam_role_policy.route_self_heal,
    aws_cloudwatch_log_group.route_self_heal,
  ]

  tags = { Name = "${var.prefix}-route-self-heal" }
}

# ── EventBridge → Lambda wiring ───────────────────────────────────────
#
# The rule `aws_cloudwatch_event_rule.route_drift` (in route_drift_alarm.tf)
# already filters DeleteRoute / ReplaceRoute on the bronze rtb. We just add
# the Lambda as a second target alongside the CloudWatch Logs target that
# already exists for forensics.

resource "aws_lambda_permission" "route_drift_invoke" {
  count         = var.vpc_peering_enabled ? 1 : 0
  statement_id  = "AllowEventBridgeRouteDrift"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.route_self_heal[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.route_drift[0].arn
}

resource "aws_cloudwatch_event_target" "route_drift_to_lambda" {
  count     = var.vpc_peering_enabled ? 1 : 0
  rule      = aws_cloudwatch_event_rule.route_drift[0].name
  target_id = "${var.prefix}-route-drift-lambda"
  arn       = aws_lambda_function.route_self_heal[0].arn
}

# ── Outputs ───────────────────────────────────────────────────────────

output "route_self_heal_function_name" {
  description = "Lambda that auto-restores the bronze CDC return route."
  value       = var.vpc_peering_enabled ? aws_lambda_function.route_self_heal[0].function_name : ""
}

output "route_self_heal_log_group" {
  description = "CW log group — every restore is logged with the offending actor."
  value       = var.vpc_peering_enabled ? aws_cloudwatch_log_group.route_self_heal[0].name : ""
}
