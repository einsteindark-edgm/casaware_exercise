# Phase E — Breadcrumb Trace Dashboard (AWS)

**Goal**: dar visibilidad punta-a-punta de cada petición que cruza la
arquitectura — qué servicios se activaron, en qué orden, con qué latencia y
con qué resultado — combinando trazas síncronas en **AWS X-Ray /
ServiceLens** con el timeline asíncrono de negocio en
`nexus_dev.silver.expense_events` (Databricks).

El usuario ya seleccionó **Enfoque D (híbrido)** y alcance **completo**
(HTTP → Temporal → datos → CDC/Medallion).

---

## Estado actual (lo que YA existe — reutilizar, no recrear)

| Pieza | Archivo | Estado |
|---|---|---|
| X-Ray SDK + middleware FastAPI | `backend/src/nexus_backend/observability/xray.py` | ✅ Instalado, gateado por `XRAY_ENABLED`. **NO** está activo en infra. |
| `current_trace_header()` (helper para propagar `Root=...;Parent=...`) | `backend/src/nexus_backend/observability/xray.py:20` | ✅ Listo para inyectarse en HTTP/Temporal headers. |
| `structlog` JSON con ContextVars (`request_id`, `tenant_id`, `user_id`, `workflow_id`) | `backend/src/nexus_backend/observability/logging.py` + `nexus-orchestration/.../observability/logging.py` | ✅ Logs ya estructurados. Falta agregar `trace_id`. |
| `tool_call_span` / `record_tool_call` | `nexus-orchestration/src/nexus_orchestration/observability/rag_metrics.py` | ✅ Mide latencia + outcome de cada tool (SQL/vector). Falta segment X-Ray. |
| CloudWatch Log Groups (3) | `infra/terraform/cloudwatch.tf` | ✅ `/ecs/nexus/{backend,frontend,worker}`, retención 14d. |
| `expense_events` (timeline negocio) | `nexus_dev.silver.expense_events` (DLT) + Mongo origin | ✅ Fuente de verdad asíncrona — append-only, 8 event types. |
| ECS Cluster | `infra/terraform/ecs_cluster.tf:6-9` | ⚠️ `containerInsights = "disabled"`. Hay que habilitarlo. |
| IAM task roles (backend, worker) | `infra/terraform/ecs_cluster.tf:73+`, `iam_worker.tf` | ⚠️ Falta `xray:PutTraceSegments` + `xray:PutTelemetryRecords`. |
| Sidecar X-Ray daemon (ADOT collector) | — | ❌ No existe. |
| Propagación `trace_id` Backend → Temporal | — | ❌ No hay (gap principal). |
| Propagación `trace_id` en Kafka/Debezium headers | — | ❌ No hay. |

**Gap real**: tenemos las piezas (SDK + structlog + events), pero ninguna
está activa en producción ni se conecta entre servicios. El plan es
*plomería*, no construcción desde cero.

---

## Decisión arquitectónica

### Por qué híbrido y no solo X-Ray

X-Ray es excelente para el tramo **síncrono** (request-response, segundos),
pero el flujo de una expensa puede tomar **horas o días** (HITL puede tardar
1–2 días). X-Ray descarta trazas tras 30 días y no está pensado para spans
de larga duración. Por eso:

- **X-Ray + ServiceLens** → tramo "tiempo real" (HTTP → Temporal →
  Bedrock/Textract/SQL/Vector). TTL útil ≤ 24h.
- **`expense_events` (Databricks)** → tramo "audit/HITL" (días). TTL = vida
  del data lake.
- **Llave de unión**: `correlation_id` que escribimos en *ambos* sistemas
  (`request_id` en logs + en `expense_events.metadata.request_id`). Permite
  saltar de la traza X-Ray al timeline `expense_events` y viceversa.

### Por qué OTEL (ADOT) en vez de X-Ray SDK puro

X-Ray SDK puro fuerza acoplamiento AWS y no instrumenta bien Temporal o
Kafka. **AWS Distro for OpenTelemetry (ADOT)** es OTel estándar con
exportador X-Ray nativo: misma destinación, mejor cobertura, portable. El
SDK X-Ray actual queda como compatibilidad — no lo arrancamos, solo dejamos
de extenderlo.

| Decisión | Por qué |
|---|---|
| ADOT collector como **sidecar** en cada ECS task | Sidecar = proceso aislado, comparte network namespace. Estándar AWS. |
| Sampling: 100% errores + 5% éxitos | Costo X-Ray = $5/M trazas. A volumen previsto (~50k req/día) = ~$8/mes. |
| `correlation_id = request_id` (ULID generado en backend) | Único, ordenable por tiempo, ya existe. |
| Headers de propagación: `traceparent` (W3C) + `x-amzn-trace-id` | W3C es estándar OTel; X-Ray header es para compatibilidad ServiceLens. |
| Kafka: header `traceparent` en cada mensaje Debezium-out | Debezium SMT permite header injection. Consumer Databricks lee y reanuda span. |

---

## Arquitectura del breadcrumb

```
┌───────────────┐
│  Frontend     │  inicia trace_id (cliente OTel JS) o lo recibe del backend
│  Next.js      │
└───────┬───────┘
        │  POST /v1/expenses     headers: traceparent=<trace_id>-<span_a>
        ▼
┌────────────────────────────┐
│  Backend FastAPI           │  span A (servicio: nexus-backend)
│  ┌──────────────────────┐  │   - request_id = <ULID>  (= correlation_id)
│  │ ADOT instrumented:    │  │   - inserts expenses + emits expense_events.created
│  │  - FastAPI            │  │     con metadata.request_id
│  │  - boto3 (S3, Textract│  │   - inicia workflow Temporal con header
│  │  - pymongo            │  │     traceparent + correlation_id
│  └──────────────────────┘  │
└────────┬───────────────────┘
         │ Temporal start_workflow(headers={traceparent, correlation_id})
         ▼
┌────────────────────────────┐
│  Temporal Worker           │  span B (servicio: nexus-worker)
│  ┌──────────────────────┐  │   - context propagator extrae traceparent
│  │ ADOT auto-instrumented│  │   - workflow_id se asocia con trace_id
│  │  - workflow runner    │  │   - cada activity = subspan
│  └──────────────────────┘  │
└────┬───────────┬───────────┘
     │           │
     ▼           ▼
 Bedrock     Textract        ← spans C, D (auto-instrumented vía botocore)
     │
     ▼
 Databricks SQL/Vector       ← span E (custom — wrap llama-sql / vs client)
     │
     │  emite expense_events.{ocr_started, ocr_completed, ...}
     │  con metadata.{request_id, trace_id, workflow_id}
     ▼
 MongoDB ── change stream ──► nexus-cdc ──► S3 (cdc bucket)
                                              │
                                              ▼ Autoloader
                                    Databricks bronze.mongodb_cdc_*
                                              │ DLT
                                              ▼
                                    silver.expense_events (con trace_id)
                                              │ join Gold materialized view
                                              ▼
                                    Dashboard Databricks
                                    "Expense Breadcrumb" (Phase E.4)
```

**Una petición → un trace_id**. Ese trace_id aparece en:
1. **CloudWatch ServiceLens** (service map + spans síncronos).
2. **CloudWatch Logs Insights** (log lines de FastAPI + worker, filtrables).
3. **`silver.expense_events`** (timeline largo, días).
4. **Dashboard Databricks "Expense Breadcrumb"** (vista negocio + técnica).

---

## Cambios por capa

### E.1 — Infra (Terraform)

Archivos a tocar:

- **`infra/terraform/ecs_cluster.tf:6-9`**:
  ```hcl
  setting {
    name  = "containerInsights"
    value = "enhanced"   # antes: "disabled"
  }
  ```

- **`infra/terraform/ecs_cluster.tf` (sección IAM)** — agregar política X-Ray
  a `worker_task` y `backend_task`:
  ```hcl
  statement {
    sid    = "XRayWrite"
    effect = "Allow"
    actions = [
      "xray:PutTraceSegments",
      "xray:PutTelemetryRecords",
      "xray:GetSamplingRules",
      "xray:GetSamplingTargets",
    ]
    resources = ["*"]
  }
  ```

- **`infra/terraform/ecs_backend.tf` y `ecs_workers.tf`** — agregar
  container sidecar ADOT al `container_definitions`:
  ```hcl
  {
    name  = "adot-collector"
    image = "public.ecr.aws/aws-observability/aws-otel-collector:v0.40.0"
    command = ["--config=/etc/ecs/ecs-default-config.yaml"]
    essential = false
    cpu       = 64
    memory    = 128
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.adot.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "adot"
      }
    }
  }
  ```
  Y env var en el container principal:
  ```hcl
  { name = "OTEL_EXPORTER_OTLP_ENDPOINT", value = "http://localhost:4317" },
  { name = "OTEL_SERVICE_NAME",           value = "nexus-backend" },   # o "nexus-worker"
  { name = "OTEL_TRACES_SAMPLER",         value = "parentbased_traceidratio" },
  { name = "OTEL_TRACES_SAMPLER_ARG",     value = "0.05" },
  { name = "OTEL_PROPAGATORS",            value = "tracecontext,xray" },
  ```

- **`infra/terraform/cloudwatch.tf`** — nuevo log group para ADOT:
  ```hcl
  resource "aws_cloudwatch_log_group" "adot" {
    name              = "/ecs/${var.prefix}/adot"
    retention_in_days = 7
  }
  ```

- **Nuevo archivo `infra/terraform/xray.tf`** — sampling rule:
  ```hcl
  resource "aws_xray_sampling_rule" "default" {
    rule_name      = "${var.prefix}-default"
    priority       = 9000
    reservoir_size = 1
    fixed_rate     = 0.05      # 5% baseline
    service_name   = "*"
    service_type   = "*"
    host           = "*"
    http_method    = "*"
    url_path       = "*"
    resource_arn   = "*"
    version        = 1
  }

  resource "aws_xray_sampling_rule" "errors_always" {
    rule_name      = "${var.prefix}-errors-always"
    priority       = 100
    reservoir_size = 5
    fixed_rate     = 1.0       # 100% para 5xx
    service_name   = "*"
    service_type   = "*"
    host           = "*"
    http_method    = "*"
    url_path       = "*"
    resource_arn   = "*"
    version        = 1
    # Nota: AWS no permite filtro por status code en sampling rules.
    # Capturamos 100% baja prioridad y dejamos que Tail Sampling del
    # collector ADOT haga el filtrado por status (config en otel.yaml).
  }
  ```

### E.2 — Backend FastAPI

Archivos a tocar:

- **Nuevo: `backend/src/nexus_backend/observability/otel.py`** — bootstrap
  OTel:
  ```python
  from opentelemetry import trace
  from opentelemetry.sdk.trace import TracerProvider
  from opentelemetry.sdk.trace.export import BatchSpanProcessor
  from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
  from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
  from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
  from opentelemetry.instrumentation.pymongo import PymongoInstrumentor
  from opentelemetry.propagators.aws import AwsXRayPropagator
  from opentelemetry.propagate import set_global_textmap
  from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator

  def install_otel(app):
      provider = TracerProvider(id_generator=AwsXRayIdGenerator())
      provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
      trace.set_tracer_provider(provider)
      set_global_textmap(AwsXRayPropagator())
      FastAPIInstrumentor.instrument_app(app)
      BotocoreInstrumentor().instrument()
      PymongoInstrumentor().instrument()
  ```

- **`backend/src/nexus_backend/main.py`** (o donde se monta la app) —
  reemplazar `install_xray(app)` por `install_otel(app)`. Mantener
  `install_xray` como no-op para no romper imports legacy.

- **`backend/src/nexus_backend/observability/logging.py`** — agregar al
  procesador de structlog un step que injecte `trace_id` desde el span
  actual:
  ```python
  def add_trace_context(_, __, event_dict):
      span = trace.get_current_span()
      if span and span.get_span_context().is_valid:
          ctx = span.get_span_context()
          event_dict["trace_id"] = format(ctx.trace_id, "032x")
          event_dict["span_id"] = format(ctx.span_id, "016x")
      return event_dict
  ```

- **`backend/src/nexus_backend/api/v1/expenses.py`** (donde se inicia el
  workflow Temporal) — pasar headers OTel al `start_workflow`:
  ```python
  from opentelemetry.propagate import inject
  carrier = {}
  inject(carrier)   # produce {"traceparent": "...", "tracestate": "..."}
  await temporal_client.start_workflow(
      ExpenseAuditWorkflow.run,
      args=[...],
      id=f"expense-audit-{expense_id}",
      task_queue="nexus-audit-tq",
      headers={k: v.encode() for k, v in carrier.items()},
  )
  ```

- **`backend/src/nexus_backend/api/v1/expenses.py`** (donde se emite
  `expense_events.created`) — incluir `trace_id` en metadata:
  ```python
  metadata = {
      "request_id": request_id_var.get(),
      "trace_id": format(trace.get_current_span().get_span_context().trace_id, "032x"),
  }
  ```

- **`backend/pyproject.toml`** — añadir deps:
  ```toml
  opentelemetry-distro = "^0.50b0"
  opentelemetry-exporter-otlp-proto-grpc = "^1.29.0"
  opentelemetry-instrumentation-fastapi = "^0.50b0"
  opentelemetry-instrumentation-botocore = "^0.50b0"
  opentelemetry-instrumentation-pymongo = "^0.50b0"
  opentelemetry-propagator-aws-xray = "^1.0.2"
  opentelemetry-sdk-extension-aws = "^2.0.2"
  ```

### E.3 — Workers Temporal

Archivos a tocar:

- **Nuevo: `nexus-orchestration/src/nexus_orchestration/observability/otel.py`**
  — espejo del backend, pero `OTEL_SERVICE_NAME=nexus-worker`.

- **`nexus-orchestration/src/nexus_orchestration/main_worker.py`** —
  llamar a `install_otel()` al arrancar y registrar un
  `TracingInterceptor` (ya existe en SDK Temporal Python desde 1.7+):
  ```python
  from temporalio.contrib.opentelemetry import TracingInterceptor
  client = await Client.connect(
      target_host=settings.temporal_host,
      interceptors=[TracingInterceptor()],
  )
  ```
  Esto extrae `traceparent` de los headers del workflow y resume el span
  automáticamente en cada workflow + activity.

- **`nexus-orchestration/src/nexus_orchestration/observability/rag_metrics.py`** —
  el `tool_call_span` actual mide latencia con `time.perf_counter()`.
  Ahora envuelve también un span OTel:
  ```python
  from opentelemetry import trace
  tracer = trace.get_tracer(__name__)

  @contextmanager
  def tool_call_span(*, tool, workflow_id=None, extra=None):
      with tracer.start_as_current_span(f"rag.tool.{tool}") as span:
          span.set_attribute("rag.tool", tool)
          if workflow_id:
              span.set_attribute("temporal.workflow_id", workflow_id)
          # ... resto igual
  ```

- **`nexus-orchestration/.../activities/sync_vector.py`** y otras
  activities que escriben a Mongo `expense_events` — incluir `trace_id`
  en `metadata` (igual que en backend).

- **`nexus-orchestration/pyproject.toml`** — mismas deps que backend +
  `opentelemetry-instrumentation-grpc` (Temporal usa gRPC).

### E.4 — CDC / Medallion (Databricks)

- **`nexus-cdc/...`** (no leído aún — TODO confirmar archivo) — al
  serializar el cambio Mongo a JSONL.gz para S3, incluir el campo
  `_trace_id` (extraído de `metadata.trace_id` que ya viajó en el
  documento Mongo desde backend/worker).

- **`nexus-medallion/src/silver/expense_events.py`** (TODO confirmar
  ruta exacta) — agregar columna `trace_id STRING` al schema DLT y
  copiarla del bronze.

- **`nexus-medallion/src/gold/expense_audit.py`** — propagar `trace_id`
  como nueva columna join'eada desde silver.

### E.5 — Dashboards

Tres superficies, cada una con su rol:

#### A) CloudWatch ServiceLens — service map en tiempo real
- Auto-generado por X-Ray a partir de los spans. **Cero config.**
- Vista: nodos = servicios (nexus-backend, nexus-worker, mongo, bedrock,
  textract, databricks-sql), edges = latencia + error rate.
- Click en nodo → drill-down a trazas individuales.
- URL: `console.aws.amazon.com/cloudwatch/home#servicelens:map`.

#### B) CloudWatch Dashboard "Nexus Breadcrumb (Live)"
Definido en Terraform (`infra/terraform/dashboards.tf`, nuevo).

Widgets:
1. **Service map embed** (link a ServiceLens).
2. **Top 20 trazas más lentas (últimas 1h)** — Logs Insights query:
   ```
   fields @timestamp, trace_id, request_id, duration_ms, status_code
   | filter @logStream like /backend/
   | sort duration_ms desc
   | limit 20
   ```
3. **Trazas con error (últimas 24h)** — filter `level = "error"`.
4. **Distribución de latencia P50/P95/P99 por endpoint** — metric math
   sobre custom metric `http.server.duration` que ADOT exporta.
5. **Buscador por `request_id`** — Logs Insights query parameterizable
   que cruza los 3 log groups (backend + worker + adot).

#### C) Dashboard Databricks "Expense Breadcrumb (audit)"
Reutiliza el dashboard `08-trace-dashboard-origen-datos.md` ya existente,
le agrega 2 widgets nuevos al final:

1. **Breadcrumb por `trace_id`** — toma `trace_id` como parámetro y
   devuelve la cadena completa de `expense_events` ordenada:
   ```sql
   SELECT
     created_at,
     event_type,
     actor,
     workflow_id,
     metadata.request_id,
     metadata.trace_id
   FROM nexus_dev.silver.expense_events
   WHERE metadata.trace_id = :trace_id
   ORDER BY created_at
   ```
2. **Latencia por etapa** — diff entre eventos consecutivos:
   ```sql
   SELECT
     event_type,
     LAG(created_at) OVER (PARTITION BY expense_id ORDER BY created_at) AS prev_at,
     created_at,
     timestampdiff(SECOND, prev_at, created_at) AS stage_duration_sec
   FROM nexus_dev.silver.expense_events
   WHERE metadata.trace_id = :trace_id
   ```

---

## Plan de implementación (5 fases)

| Fase | Entregable | LOE | Bloquea a |
|---|---|---|---|
| **E.1 Infra** | Container Insights ON, IAM X-Ray, sampling rules, ADOT log group | 0.5 día | E.2, E.3 |
| **E.2 Backend** | `install_otel()`, propagación a Temporal, trace_id en logs + expense_events | 1 día | E.4 |
| **E.3 Workers** | `TracingInterceptor`, span en `tool_call_span`, trace_id en eventos | 1 día | E.4 |
| **E.4 Medallion** | `trace_id` en bronze→silver→gold | 0.5 día | E.5 dashboard C |
| **E.5 Dashboards** | ServiceLens (auto), CW Dashboard custom, Databricks widgets | 1 día | — |

**Total**: ~4 días-persona. Recomiendo merge incremental (cada fase = un PR).

---

## Costos estimados (us-east-1, dev)

Asumiendo 50k requests/día (~1.5M/mes) y sampling 5% éxitos + 100% errores (~80k trazas/mes):

| Servicio | Pricing | Costo mensual |
|---|---|---|
| X-Ray traces | $5 / 1M trazas + $0.50 / 1M scans | ~$2 |
| CloudWatch Container Insights (Enhanced) | $0.20 / contenedor / hora | ~$30 (5 containers × 730h) |
| CloudWatch Logs Insights queries | $0.005 / GB scanned | ~$5 |
| ADOT collector compute (sidecar 64 CPU/128 MB × 5 tasks) | Fargate ~$0.04/h | ~$15 |
| **Total** | | **~$52/mes** |

Si el costo de Container Insights Enhanced ($30/mes) es prohibitivo en
dev, downgrade a `enabled` (basic) baja a ~$5/mes — pierdes métricas a
nivel de container pero conservas service map.

---

## Validación / runbook

Después de cada fase:

**E.1**: `terraform plan` muestra solo los cambios esperados; `terraform
apply` no rompe deploy actual.

**E.2**: deploy backend, hacer `curl POST /v1/expenses ... -i`, ver header
`x-amzn-trace-id` en respuesta. En CloudWatch Logs Insights:
```
fields @timestamp, trace_id, request_id, msg
| filter request_id = "<id-de-la-petición>"
```
debe devolver al menos 3 líneas con el mismo `trace_id`.

**E.3**: arrancar un workflow, ir a ServiceLens → ver edge
`nexus-backend → nexus-worker` con la traza completa.

**E.4**: query Databricks:
```sql
SELECT trace_id FROM nexus_dev.silver.expense_events
WHERE created_at > current_timestamp() - INTERVAL 1 HOUR
  AND trace_id IS NOT NULL
LIMIT 10
```
debe devolver trace_ids no-nulos.

**E.5**: pegar un `trace_id` real en el dashboard Databricks "Expense
Breadcrumb" → ver timeline completo. Cruzar con ServiceLens → mismo
trace_id, misma cadena.

---

## Riesgos y mitigación

| Riesgo | Mitigación |
|---|---|
| ADOT collector falla → spans se pierden silenciosamente | Configurar `OTEL_LOG_LEVEL=warn` y monitorear `/ecs/nexus/adot` log group. Alarma CloudWatch si error rate > 1%. |
| Sampling 5% pierde trazas relevantes | `errors_always` rule captura 100% de 5xx. Para investigaciones puntuales, subir temporalmente a 100% via `aws_xray_sampling_rule.fixed_rate`. |
| Costo Container Insights Enhanced > presupuesto | Downgrade a `enabled` (basic). Pierdes métricas de container pero conservas service map y trazas. |
| Temporal `TracingInterceptor` no propaga si workflow se reanuda tras failover | Conocido. El span padre se pierde si el worker muere; el span hijo aparece como root nuevo. Aceptable — `correlation_id` (request_id) sigue uniendo en logs. |
| Inyección de `traceparent` en Debezium/Kafka requiere SMT custom | Postergar a Phase E.6 si MSK aún no está activo (`msk_enabled=false`). El tramo CDC queda visible solo via `expense_events.metadata.trace_id` (que sí viaja por Mongo). |
| `metadata.trace_id` en Mongo expone IDs internos a auditores | `trace_id` no es PII y es opaco. Documentar en `10-autenticacion-autorizacion-tenant.md`. |

---

## Out of scope (futuras fases)

- **E.6** — Propagación trace_id en headers Kafka/Debezium (cuando MSK
  esté activo).
- **E.7** — Frontend Next.js: instrumentación OTel JS para iniciar
  trace_id desde el navegador (RUM).
- **E.8** — Anomaly detection sobre métricas X-Ray (CloudWatch Anomaly
  Detector sobre `ResponseTime`).
- **E.9** — Integración con Atlas Charts (`07-tablero-ops-atlas-charts.md`)
  para correlacionar `request_id` con métricas Mongo.

---

## Resumen ejecutivo

**Sí es factible.** Tienes el 60% de las piezas (X-Ray SDK, structlog,
expense_events, CloudWatch Logs). Lo que falta es **plomería**: activar
Container Insights, migrar de X-Ray SDK a ADOT, propagar `traceparent`
de backend a workers, y agregar `trace_id` a la columna de
`expense_events`. ~4 días, ~$52/mes en infra. Los dashboards finales son
3: ServiceLens (auto), CloudWatch Dashboard (Terraform), Databricks
Dashboard (extensión del existente).
