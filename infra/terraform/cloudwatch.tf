# ── CloudWatch Log Groups (Phase A.8) ────────────────────────────────

resource "aws_cloudwatch_log_group" "backend" {
  name              = "/ecs/${var.prefix}/backend"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "frontend" {
  name              = "/ecs/${var.prefix}/frontend"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/ecs/${var.prefix}/worker"
  retention_in_days = 14
}

# Phase E.1 — ADOT collector sidecar logs (5xx tracing diagnostics).
resource "aws_cloudwatch_log_group" "adot" {
  name              = "/ecs/${var.prefix}/adot"
  retention_in_days = 7
}
