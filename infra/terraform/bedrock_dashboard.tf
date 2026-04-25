# ── Phase E.6 — CloudWatch Dashboard "Nexus Agent (Bedrock)" ─────────
#
# Agent-focused view: what the model decides (tool_use), how many turns
# it takes, how many tokens it consumes, which tools fail. Combines:
#  - Bedrock Invocation Logs (full prompts + responses)
#  - Worker bridge log (`bedrock.invoke` with trace_id)
#  - AWS/Bedrock metrics (latency, tokens, errors)
#
# Join key with the breadcrumb dashboard: trace_id (input variable).

resource "aws_cloudwatch_dashboard" "agent" {
  dashboard_name = "${var.prefix}-agent-bedrock"

  dashboard_body = jsonencode({
    variables = [
      {
        type         = "pattern"
        pattern      = "PASTE-TRACE-ID-HERE"
        inputType    = "input"
        id           = "trace_id"
        label        = "trace_id"
        defaultValue = "__pick_a_trace_id__"
        visible      = true
      },
    ]

    widgets = [
      # ── Header ──────────────────────────────────────────────────
      {
        type   = "text"
        x      = 0
        y      = 0
        width  = 24
        height = 3
        properties = {
          markdown = "# Nexus Agent (Bedrock)\n\nWhat the model decides on each turn: tools it invokes, tokens consumed, latency, errors. To see the full service chain of a request go to the [Nexus Breadcrumb dashboard](https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards:name=${var.prefix}-breadcrumb-live).\n\n**How to use**: pick a `trace_id` from the _Recent agent invocations_ table below and paste it in the input at the top — every widget filters automatically. The _Prompts and responses_ widget pulls the full text from Bedrock Invocation Logs."
        }
      },

      # ── Row 1: catalog of recent agent invocations (THE PICKER) ──
      {
        type   = "log"
        x      = 0
        y      = 3
        width  = 24
        height = 8
        properties = {
          title  = "🗂️ Recent agent invocations — copy a trace_id from here (last hour)"
          region = var.aws_region
          query  = "SOURCE '${aws_cloudwatch_log_group.worker.name}' | fields @timestamp, trace_id, workflow_id, model_id, mode, stop_reason, tools_emitted_count, input_tokens, output_tokens, latency_ms | filter event = \"bedrock.invoke\" | sort @timestamp desc | limit 50"
          view   = "table"
        }
      },

      # ── Row 2: aggregated decisions (no filter) ─────────────────
      {
        type   = "log"
        x      = 0
        y      = 11
        width  = 12
        height = 8
        properties = {
          title  = "🎯 Tools the model chose to invoke (last hour)"
          region = var.aws_region
          query  = "SOURCE '${aws_cloudwatch_log_group.worker.name}' | fields @timestamp | filter event = \"bedrock.invoke\" and tools_emitted_count > 0 | parse tools_emitted /\"name\":\"(?<tool_name>[^\"]+)\"/ | stats count() as times by tool_name | sort times desc"
          view   = "pie"
        }
      },
      {
        type   = "log"
        x      = 12
        y      = 11
        width  = 12
        height = 8
        properties = {
          title  = "Stop reasons (end_turn / tool_use / max_tokens / error)"
          region = var.aws_region
          query  = "SOURCE '${aws_cloudwatch_log_group.worker.name}' | filter event = \"bedrock.invoke\" | stats count() as times by stop_reason | sort times desc"
          view   = "bar"
        }
      },

      # ── Row 3: per trace_id (decision by decision) ──────────────
      {
        type   = "log"
        x      = 0
        y      = 19
        width  = 24
        height = 10
        properties = {
          title  = "🧠 Model decisions for the selected trace_id (turn-by-turn timeline)"
          region = var.aws_region
          query  = "SOURCE '${aws_cloudwatch_log_group.worker.name}' | fields @timestamp, model_id, mode, stop_reason, tools_offered, tools_emitted, tools_emitted_count, input_tokens, output_tokens, text_chars, latency_ms, workflow_id | filter event = \"bedrock.invoke\" and trace_id = \"PASTE-TRACE-ID-HERE\" | sort @timestamp asc | limit 50"
          view   = "table"
        }
      },

      # ── Row 4: full prompt + response (Bedrock Invocation Logs) ─
      {
        type   = "log"
        x      = 0
        y      = 29
        width  = 24
        height = 14
        properties = {
          title  = "📜 Full prompts and model responses (Bedrock Invocation Logs — narrow the time range to your trace's window)"
          region = var.aws_region
          # Bedrock logs don't carry trace_id directly. Workaround: align
          # the dashboard's time range to the window of the selected trace
          # (use the bridge log timestamps in the row above) and this widget
          # surfaces every invocation in that window.
          query = "SOURCE '${aws_cloudwatch_log_group.bedrock_invocations.name}' | fields @timestamp, modelId, operation, input.inputContentType, output.outputContentType, input.inputBodyJson.messages, output.outputBodyJson.output.message.content, output.outputBodyJson.stopReason, output.outputBodyJson.usage.inputTokens, output.outputBodyJson.usage.outputTokens | sort @timestamp desc | limit 20"
          view  = "table"
        }
      },

      # ── Row 5: turns per workflow + tool input shapes ──────────
      {
        type   = "log"
        x      = 0
        y      = 43
        width  = 12
        height = 8
        properties = {
          title  = "🔁 Agentic loop iterations per workflow (turns until final answer)"
          region = var.aws_region
          query  = "SOURCE '${aws_cloudwatch_log_group.worker.name}' | filter event = \"bedrock.invoke\" | stats count() as turns by workflow_id | sort turns desc | limit 20"
          view   = "table"
        }
      },
      {
        type   = "log"
        x      = 12
        y      = 43
        width  = 12
        height = 8
        properties = {
          title  = "💬 Most common tool input keys (how the model calls each tool)"
          region = var.aws_region
          query  = "SOURCE '${aws_cloudwatch_log_group.worker.name}' | fields @timestamp | filter event = \"bedrock.invoke\" and tools_emitted_count > 0 | parse tools_emitted /\"name\":\"(?<tool>[^\"]+)\",\"input_keys\":\\[(?<keys>[^\\]]*)\\]/ | stats count() as times by tool, keys | sort times desc | limit 20"
          view   = "table"
        }
      },

      # ── Row 6: Bedrock metrics (all invocations) ────────────────
      {
        type   = "metric"
        x      = 0
        y      = 51
        width  = 8
        height = 6
        properties = {
          title   = "Invocation latency (P50 / P95 / P99)"
          region  = var.aws_region
          view    = "timeSeries"
          stacked = false
          metrics = [
            ["AWS/Bedrock", "InvocationLatency", "ModelId", "us.amazon.nova-pro-v1:0", { stat = "p50", label = "P50" }],
            ["...", { stat = "p95", label = "P95" }],
            ["...", { stat = "p99", label = "P99" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 8
        y      = 51
        width  = 8
        height = 6
        properties = {
          title   = "Tokens (input + output)"
          region  = var.aws_region
          view    = "timeSeries"
          stacked = true
          metrics = [
            ["AWS/Bedrock", "InputTokenCount", "ModelId", "us.amazon.nova-pro-v1:0", { stat = "Sum", label = "Input" }],
            ["AWS/Bedrock", "OutputTokenCount", "ModelId", "us.amazon.nova-pro-v1:0", { stat = "Sum", label = "Output" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 16
        y      = 51
        width  = 8
        height = 6
        properties = {
          title   = "Errors and throttles"
          region  = var.aws_region
          view    = "timeSeries"
          stacked = false
          metrics = [
            ["AWS/Bedrock", "InvocationClientErrors", "ModelId", "us.amazon.nova-pro-v1:0", { stat = "Sum", label = "Client errors (4xx)" }],
            ["AWS/Bedrock", "InvocationServerErrors", "ModelId", "us.amazon.nova-pro-v1:0", { stat = "Sum", label = "Server errors (5xx)" }],
            ["AWS/Bedrock", "InvocationThrottles", "ModelId", "us.amazon.nova-pro-v1:0", { stat = "Sum", label = "Throttles" }],
          ]
        }
      },

      # ── Row 7: estimated cost (Nova Pro pricing — Apr 2026) ─────
      {
        type   = "metric"
        x      = 0
        y      = 57
        width  = 24
        height = 6
        properties = {
          title   = "💰 Estimated cost per hour (USD — Nova Pro: $0.0008/1k input, $0.0032/1k output)"
          region  = var.aws_region
          view    = "timeSeries"
          stacked = true
          metrics = [
            [{ expression = "SUM(METRICS('input'))*0.0000008", label = "Input cost ($)", id = "in_cost" }],
            [{ expression = "SUM(METRICS('output'))*0.0000032", label = "Output cost ($)", id = "out_cost" }],
            [{ expression = "in_cost + out_cost", label = "Total ($)", id = "total" }],
            ["AWS/Bedrock", "InputTokenCount", "ModelId", "us.amazon.nova-pro-v1:0", { id = "input", stat = "Sum", visible = false }],
            ["AWS/Bedrock", "OutputTokenCount", "ModelId", "us.amazon.nova-pro-v1:0", { id = "output", stat = "Sum", visible = false }],
          ]
        }
      },
    ]
  })
}

output "agent_dashboard_url" {
  description = "URL of the Bedrock agent CloudWatch dashboard."
  value       = "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards:name=${aws_cloudwatch_dashboard.agent.dashboard_name}"
}
