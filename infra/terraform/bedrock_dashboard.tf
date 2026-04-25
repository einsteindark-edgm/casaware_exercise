# ── Phase E.6 — CloudWatch Dashboard "Nexus Agent (Bedrock)" ─────────
#
# Vista enfocada en el agente: qué decide el modelo (tool_use), cuántos
# turnos toma, cuántos tokens consume, qué tools fallan. Combina:
#  - Bedrock Invocation Logs (prompts + respuestas completas)
#  - Log puente del worker (`bedrock.invoke` con trace_id)
#  - Métricas AWS/Bedrock (latencia, tokens, errores)
#
# Llave de unión con el dashboard breadcrumb: trace_id (input variable).

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
          markdown = "# Nexus Agent (Bedrock)\n\nQué decide el modelo en cada turno: tools que invoca, tokens consumidos, latencia, errores. Para ver la cadena completa de servicios de una petición ir al dashboard [Nexus Breadcrumb](https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards:name=${var.prefix}-breadcrumb-live).\n\n**Cómo usar**: pegá un `trace_id` arriba para ver QUÉ decidió el modelo en esa petición específica. El widget _Prompts y respuestas_ accede a Bedrock Invocation Logs (texto completo)."
        }
      },

      # ── Row 1: decisiones agregadas (sin filtro) ────────────────
      {
        type   = "log"
        x      = 0
        y      = 3
        width  = 12
        height = 8
        properties = {
          title  = "🎯 Tools que el modelo decidió usar (última hora)"
          region = var.aws_region
          query  = "SOURCE '${aws_cloudwatch_log_group.worker.name}' | fields @timestamp | filter event = \"bedrock.invoke\" and tools_emitted_count > 0 | parse tools_emitted /\"name\":\"(?<tool_name>[^\"]+)\"/ | stats count() as veces by tool_name | sort veces desc"
          view   = "pie"
        }
      },
      {
        type   = "log"
        x      = 12
        y      = 3
        width  = 12
        height = 8
        properties = {
          title  = "Stop reasons (end_turn / tool_use / max_tokens / error)"
          region = var.aws_region
          query  = "SOURCE '${aws_cloudwatch_log_group.worker.name}' | filter event = \"bedrock.invoke\" | stats count() as veces by stop_reason | sort veces desc"
          view   = "bar"
        }
      },

      # ── Row 2: por trace_id (decisión a decisión) ───────────────
      {
        type   = "log"
        x      = 0
        y      = 11
        width  = 24
        height = 10
        properties = {
          title  = "🧠 Decisiones del modelo en este trace_id (cronología de invocaciones)"
          region = var.aws_region
          query  = "SOURCE '${aws_cloudwatch_log_group.worker.name}' | fields @timestamp, model_id, mode, stop_reason, tools_offered, tools_emitted, tools_emitted_count, input_tokens, output_tokens, text_chars, latency_ms, workflow_id | filter event = \"bedrock.invoke\" and trace_id = \"PASTE-TRACE-ID-HERE\" | sort @timestamp asc | limit 50"
          view   = "table"
        }
      },

      # ── Row 3: prompt + respuesta completa (Bedrock Invocation Logs) ──
      {
        type   = "log"
        x      = 0
        y      = 21
        width  = 24
        height = 14
        properties = {
          title  = "📜 Prompts y respuestas del modelo (Bedrock Invocation Logs — filtrado por timestamp del trace)"
          region = var.aws_region
          # Bedrock logs no traen trace_id directamente — los filtramos por
          # ventana cercana al log puente. El usuario ajusta el time range
          # del dashboard al rango del trace y este widget muestra todo lo
          # de Bedrock en esa ventana.
          query = "SOURCE '${aws_cloudwatch_log_group.bedrock_invocations.name}' | fields @timestamp, modelId, operation, input.inputContentType, output.outputContentType, input.inputBodyJson.messages, output.outputBodyJson.output.message.content, output.outputBodyJson.stopReason, output.outputBodyJson.usage.inputTokens, output.outputBodyJson.usage.outputTokens | sort @timestamp desc | limit 20"
          view  = "table"
        }
      },

      # ── Row 4: turnos por workflow (agentic loop iterations) ────
      {
        type   = "log"
        x      = 0
        y      = 35
        width  = 12
        height = 8
        properties = {
          title  = "🔁 Iteraciones del agentic loop por workflow (turnos antes de respuesta final)"
          region = var.aws_region
          query  = "SOURCE '${aws_cloudwatch_log_group.worker.name}' | filter event = \"bedrock.invoke\" | stats count() as turnos by workflow_id | sort turnos desc | limit 20"
          view   = "table"
        }
      },
      {
        type   = "log"
        x      = 12
        y      = 35
        width  = 12
        height = 8
        properties = {
          title  = "💬 Tools input_keys más comunes (cómo llama el modelo a cada tool)"
          region = var.aws_region
          query  = "SOURCE '${aws_cloudwatch_log_group.worker.name}' | fields @timestamp | filter event = \"bedrock.invoke\" and tools_emitted_count > 0 | parse tools_emitted /\"name\":\"(?<tool>[^\"]+)\",\"input_keys\":\\[(?<keys>[^\\]]*)\\]/ | stats count() as veces by tool, keys | sort veces desc | limit 20"
          view   = "table"
        }
      },

      # ── Row 5: métricas Bedrock (todas las invocaciones) ────────
      {
        type   = "metric"
        x      = 0
        y      = 43
        width  = 8
        height = 6
        properties = {
          title   = "Latencia de invocación (P50/P95/P99)"
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
        y      = 43
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
        y      = 43
        width  = 8
        height = 6
        properties = {
          title   = "Errores y throttles"
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

      # ── Row 6: costo estimado (Nova Pro pricing — abril 2026) ───
      {
        type   = "metric"
        x      = 0
        y      = 49
        width  = 24
        height = 6
        properties = {
          title   = "💰 Costo estimado por hora ($USD — Nova Pro: $0.0008/1k input, $0.0032/1k output)"
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
  description = "URL del dashboard CloudWatch del agente Bedrock."
  value       = "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards:name=${aws_cloudwatch_dashboard.agent.dashboard_name}"
}
