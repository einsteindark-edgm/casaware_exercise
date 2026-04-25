# ── Phase E.5 — CloudWatch Dashboard "Nexus Breadcrumb (Live)" ───────
#
# El breadcrumb se construye desde los LOGS estructurados (no desde X-Ray).
# Los logs son 100% — toda ejecución aparece, sin sampling. ServiceLens
# sigue siendo útil para el grafo visual auto-generado, pero como solo
# captura el 5% de las trazas en dev, lo complementamos aquí con widgets
# que cubren todo el tráfico.
#
# Variables del dashboard: pegá un trace_id o request_id en los inputs de
# arriba — los widgets se filtran automáticamente.

resource "aws_cloudwatch_dashboard" "breadcrumb" {
  dashboard_name = "${var.prefix}-breadcrumb-live"

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
      # ── Header ──────────────────────────────────────────────────
      {
        type   = "text"
        x      = 0
        y      = 0
        width  = 24
        height = 3
        properties = {
          markdown = "# Nexus Breadcrumb (Live)\n\n**Cómo usar este tablero**:\n1. Mirá el widget _Ejecuciones recientes_ (abajo a la izquierda) y copiá un `trace_id`.\n2. Pegalo en el input `trace_id` de la barra superior.\n3. Los widgets _Cadena de componentes_ y _Distribución de tiempo_ muestran qué se ejecutó en esa petición y cuánto tardó cada parte.\n\nEl breadcrumb se construye desde **logs estructurados** — toda ejecución aparece, sin depender de X-Ray sampling.\n\n[ServiceLens (grafo visual, 5% sampling)](https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#servicelens:map) · [X-Ray Traces](https://${var.aws_region}.console.aws.amazon.com/xray/home?region=${var.aws_region}#/traces)"
        }
      },

      # ── Row 1: ejecuciones recientes (catálogo de trace_ids) ──
      {
        type   = "log"
        x      = 0
        y      = 3
        width  = 12
        height = 8
        properties = {
          title  = "Ejecuciones recientes (copiá un trace_id de aquí, última hora)"
          region = var.aws_region
          query  = "SOURCE '${aws_cloudwatch_log_group.backend.name}' | SOURCE '${aws_cloudwatch_log_group.worker.name}' | fields trace_id, @logStream | filter ispresent(trace_id) and trace_id != \"\" | stats count() as eventos, earliest(@timestamp) as inicio, latest(@timestamp) as fin, count_distinct(@logStream) as servicios by trace_id | sort fin desc | limit 30"
          view   = "table"
        }
      },
      {
        type   = "log"
        x      = 12
        y      = 3
        width  = 12
        height = 8
        properties = {
          title  = "Últimas peticiones HTTP (backend, copiá un request_id si querés)"
          region = var.aws_region
          query  = "SOURCE '${aws_cloudwatch_log_group.backend.name}' | fields @timestamp, request_id, trace_id, method, path, status_code, elapsed_ms | filter event = \"request.completed\" | sort @timestamp desc | limit 30"
          view   = "table"
        }
      },

      # ── Row 2: el breadcrumb completo (cadena de componentes) ──
      {
        type   = "log"
        x      = 0
        y      = 11
        width  = 24
        height = 12
        properties = {
          title  = "🍞 Breadcrumb — cadena de componentes activados (filtrado por trace_id)"
          region = var.aws_region
          query  = "SOURCE '${aws_cloudwatch_log_group.backend.name}' | SOURCE '${aws_cloudwatch_log_group.worker.name}' | fields @timestamp, coalesce(strcontains(@logStream, \"backend\"), 0) as is_backend, @logStream as stream, event, method, path, status_code, elapsed_ms, workflow_id, tool, outcome, latency_ms, row_count, msg, error | filter trace_id = \"PASTE-TRACE-ID-HERE\" or request_id = \"PASTE-REQUEST-ID-HERE\" | sort @timestamp asc | limit 500"
          view   = "table"
        }
      },

      # ── Row 3: distribución de tiempo por componente ───────────
      {
        type   = "log"
        x      = 0
        y      = 23
        width  = 12
        height = 8
        properties = {
          title  = "⏱️ Tiempo por componente (suma de latency_ms por evento — trace_id seleccionado)"
          region = var.aws_region
          query  = "SOURCE '${aws_cloudwatch_log_group.backend.name}' | SOURCE '${aws_cloudwatch_log_group.worker.name}' | fields coalesce(tool, event) as componente, coalesce(latency_ms, elapsed_ms) as ms | filter trace_id = \"PASTE-TRACE-ID-HERE\" and ispresent(ms) | stats sum(ms) as total_ms, count() as veces by componente | sort total_ms desc | limit 30"
          view   = "bar"
        }
      },
      {
        type   = "log"
        x      = 12
        y      = 23
        width  = 12
        height = 8
        properties = {
          title  = "Resultado de cada tool RAG (ok / empty / error — trace_id seleccionado)"
          region = var.aws_region
          query  = "SOURCE '${aws_cloudwatch_log_group.worker.name}' | fields tool, outcome | filter trace_id = \"PASTE-TRACE-ID-HERE\" and ispresent(tool) | stats count() as veces by tool, outcome | sort veces desc"
          view   = "pie"
        }
      },

      # ── Row 4: errores recientes (siempre, sin filtro) ─────────
      {
        type   = "log"
        x      = 0
        y      = 31
        width  = 24
        height = 6
        properties = {
          title  = "🚨 Errores recientes (backend + worker, últimas 24h — sin filtro de trace)"
          region = var.aws_region
          query  = "SOURCE '${aws_cloudwatch_log_group.backend.name}' | SOURCE '${aws_cloudwatch_log_group.worker.name}' | fields @timestamp, trace_id, request_id, workflow_id, level, event, error, msg | filter level = \"error\" or level = \"exception\" | sort @timestamp desc | limit 50"
          view   = "table"
        }
      },

      # ── Row 5: métricas X-Ray (los pocos que sí samplean) ─────
      {
        type   = "metric"
        x      = 0
        y      = 37
        width  = 12
        height = 6
        properties = {
          title   = "X-Ray latencia P50/P95/P99 (nexus-backend, solo trazas sampleadas)"
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
        y      = 37
        width  = 12
        height = 6
        properties = {
          title   = "X-Ray error rate (backend + worker, solo trazas sampleadas)"
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
