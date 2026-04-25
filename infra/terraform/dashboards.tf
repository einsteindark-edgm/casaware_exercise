# ── Phase E.5 — CloudWatch Dashboard "Nexus Breadcrumb (Live)" ───────
#
# Vista en tiempo real del breadcrumb de cada petición:
#  - Service map link (ServiceLens auto-generated)
#  - Top trazas lentas y trazas con error (Logs Insights)
#  - Distribución de latencia P50/P95/P99 por endpoint
#  - Buscador por request_id que cruza backend + worker logs
#
# Para correlacionar con el dashboard Databricks "Expense Breadcrumb",
# usar trace_id como llave (presente en logs JSON de ambos servicios).

resource "aws_cloudwatch_dashboard" "breadcrumb" {
  dashboard_name = "${var.prefix}-breadcrumb-live"

  dashboard_body = jsonencode({
    # Phase E.5b — variables de dashboard. CloudWatch reemplaza el `pattern`
    # por el valor del input en VIVO en todas las queries. Cambiar el valor
    # del input en la cabecera del dashboard re-ejecuta los widgets que
    # contengan ese pattern. Por defecto deja un valor "no-match" para que
    # los widgets no devuelvan filas hasta que el usuario pegue un id real.
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
      {
        type         = "pattern"
        pattern      = "PASTE-REQUEST-ID-HERE"
        inputType    = "input"
        id           = "request_id"
        label        = "request_id"
        defaultValue = "__pick_a_request_id__"
        visible      = true
      },
    ]

    widgets = [
      {
        type   = "text"
        x      = 0
        y      = 0
        width  = 24
        height = 2
        properties = {
          markdown = "# Nexus Breadcrumb (Live)\n\nTrazas síncronas de cada petición — del frontend al worker, con latencia y errores. Para el timeline largo (HITL, días) ir al dashboard Databricks `Expense Breadcrumb`.\n\n**Cómo usar**: pega un `trace_id` o `request_id` en los inputs de la barra superior — los widgets se filtran automáticamente. Encuentra `trace_id`s en el widget _Top trazas_ o en [X-Ray Traces](https://${var.aws_region}.console.aws.amazon.com/xray/home?region=${var.aws_region}#/traces). **Service Map**: [Abrir ServiceLens](https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#servicelens:map)."
        }
      },
      {
        type   = "log"
        x      = 0
        y      = 2
        width  = 12
        height = 8
        properties = {
          title  = "Top 20 trazas más lentas (backend, última hora)"
          region = var.aws_region
          query  = "SOURCE '${aws_cloudwatch_log_group.backend.name}' | fields @timestamp, trace_id, request_id, path, elapsed_ms, status_code | filter ispresent(elapsed_ms) | sort elapsed_ms desc | limit 20"
          view   = "table"
        }
      },
      {
        type   = "log"
        x      = 12
        y      = 2
        width  = 12
        height = 8
        properties = {
          title  = "Errores recientes (backend + worker, últimas 24h)"
          region = var.aws_region
          query  = "SOURCE '${aws_cloudwatch_log_group.backend.name}' | SOURCE '${aws_cloudwatch_log_group.worker.name}' | fields @timestamp, trace_id, request_id, workflow_id, level, event, error | filter level = \"error\" or level = \"exception\" | sort @timestamp desc | limit 50"
          view   = "table"
        }
      },
      {
        type   = "log"
        x      = 0
        y      = 10
        width  = 24
        height = 10
        properties = {
          title  = "Breadcrumb por trace_id (pega el trace_id en la query — backend + worker + adot)"
          region = var.aws_region
          query  = "SOURCE '${aws_cloudwatch_log_group.backend.name}' | SOURCE '${aws_cloudwatch_log_group.worker.name}' | SOURCE '${aws_cloudwatch_log_group.adot.name}' | fields @timestamp, @logStream, trace_id, span_id, request_id, workflow_id, event, msg, elapsed_ms, status_code | filter trace_id = \"PASTE-TRACE-ID-HERE\" | sort @timestamp asc | limit 200"
          view   = "table"
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 20
        width  = 12
        height = 6
        properties = {
          title   = "X-Ray latencia P50/P95/P99 (nexus-backend)"
          region  = var.aws_region
          view    = "timeSeries"
          stacked = false
          metrics = [
            ["AWS/X-Ray", "ResponseTime", "ServiceName", "nexus-backend", "ServiceType", "AWS::ECS::Container", { stat = "p50", label = "P50" }],
            ["...", { stat = "p95", label = "P95" }],
            ["...", { stat = "p99", label = "P99" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 20
        width  = 12
        height = 6
        properties = {
          title   = "X-Ray error rate (backend + worker)"
          region  = var.aws_region
          view    = "timeSeries"
          stacked = false
          metrics = [
            ["AWS/X-Ray", "ErrorRate", "ServiceName", "nexus-backend", { stat = "Average", label = "backend" }],
            ["AWS/X-Ray", "ErrorRate", "ServiceName", "nexus-worker", { stat = "Average", label = "worker" }],
            ["AWS/X-Ray", "FaultRate", "ServiceName", "nexus-backend", { stat = "Average", label = "backend 5xx" }],
            ["AWS/X-Ray", "FaultRate", "ServiceName", "nexus-worker", { stat = "Average", label = "worker 5xx" }],
          ]
        }
      },
      {
        type   = "log"
        x      = 0
        y      = 26
        width  = 24
        height = 8
        properties = {
          title  = "Buscador por request_id (alternativo si X-Ray sampling lo descartó)"
          region = var.aws_region
          query  = "SOURCE '${aws_cloudwatch_log_group.backend.name}' | SOURCE '${aws_cloudwatch_log_group.worker.name}' | fields @timestamp, @logStream, trace_id, request_id, workflow_id, event, msg | filter request_id = \"PASTE-REQUEST-ID-HERE\" | sort @timestamp asc | limit 200"
          view   = "table"
        }
      },
    ]
  })
}

output "breadcrumb_dashboard_url" {
  description = "URL del dashboard CloudWatch breadcrumb."
  value       = "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards:name=${aws_cloudwatch_dashboard.breadcrumb.dashboard_name}"
}

output "service_lens_url" {
  description = "URL de CloudWatch ServiceLens (service map automático)."
  value       = "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#servicelens:map"
}
