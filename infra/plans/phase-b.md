# Phase B · CDC Mongo → Bronze (dev-tier)

## Objetivo

Reemplazar el notebook batch `seed_bronze_from_mongo.py` por un pipeline **continuo** que capture cada insert/update/delete en Mongo Atlas y lo materialice en `bronze.mongodb_cdc_*` para que silver/gold se mantengan vivos sin correr el seed manualmente.

---

## Arquitectura: "Debezium-lite"

Doc 04 propone Debezium Server + Kafka + Kafka Connect + S3 Sink. En prod real eso es correcto (MSK ~$150/mo + Kafka Connect ECS ~$30/mo + ops). Para un dev env con presupuesto `< $20/mo` y sin requerimientos de multi-consumer ni replay a offsets arbitrarios, implementamos el mismo patrón con una pieza menos:

```
MongoDB Atlas (replica set, change streams)
    │
    │  pymongo.watch([$match]) × 5 colecciones
    ▼
ECS Fargate: nexus-cdc-listener   (1 task, siempre arriba)
    │
    │  - resume_token persistido cada batch → DynamoDB (nexus-dev-edgm-cdc-offsets)
    │  - buffering en memoria (30s o 200 eventos → flush)
    │  - dead-letters → s3://.../dlq/...
    ▼
S3 bucket: nexus-dev-edgm-cdc/
  ├── expenses/year=YYYY/month=MM/day=DD/hour=HH/batch_<ULID>.jsonl.gz
  ├── receipts/...
  ├── hitl_tasks/...
  ├── ocr_extractions/...
  ├── expense_events/...
  └── dlq/...
    │
    │  Databricks Autoloader (cloudFiles, JSON, schema hints explicitos)
    ▼
Databricks Lakeflow: bronze_cdc pipeline (triggered, serverless)
  → bronze.mongodb_cdc_expenses          (append-only, Delta, CDF=on)
  → bronze.mongodb_cdc_receipts
  → bronze.mongodb_cdc_hitl_tasks
  → bronze.mongodb_cdc_ocr_extractions
  → bronze.mongodb_cdc_expense_events
    │
    │  silver pipeline (YA EXISTE, sin cambios)
    ▼
silver.* → gold.* → vector.expense_chunks_index
```

Por qué "Debezium-lite" es la elección correcta para este dev:

| Aspecto | Debezium+MSK (doc 04) | Este approach |
|---|---|---|
| Costo mensual | ~$200 | ~$12 (1 ECS task + S3 + DDB PPR) |
| Latencia end-to-end Mongo→bronze | ~3s | ~35s (30s batcher + ~5s autoloader trigger) |
| Replay arbitrario | Sí, a cualquier offset | No, solo desde último resume_token DDB |
| Multi-consumer | Sí (cualquiera lee de Kafka) | No (solo bronze lee S3) |
| Schema registry | Avro + Confluent SR | Explícito en notebooks + schema hints |
| Operación | Ops Kafka + Connect + Debezium | 1 servicio Python + 1 bucket |
| Migración a prod | Ya está | Swap a Debezium Server → Kinesis/MSK: mismo envelope |

El envelope JSON es **idéntico** al de Debezium con `ExtractNewDocumentState` unwrap:
`{ <doc fields>, __op: 'c'|'u'|'d'|'r', __source_ts_ms: int, __deleted: bool }`.
Esto garantiza que el día que montemos Debezium+MSK las bronze tables no cambian — solo cambia el productor.

---

## Componentes a crear

### 1. `nexus-cdc/` — servicio Python (ECS Fargate)

```
nexus-cdc/
├── pyproject.toml                       # deps: pymongo, boto3, structlog, pydantic-settings, python-ulid
├── Dockerfile                           # uv multi-stage, python:3.12-slim
├── README.md
├── src/nexus_cdc/
│   ├── __init__.py
│   ├── config.py                        # Settings: MONGODB_URI, DDB_TABLE, S3_BUCKET, BATCH_SIZE, BATCH_SECONDS, COLLECTIONS
│   ├── main.py                          # Entry: asyncio.gather(listen(coll) for coll in settings.collections)
│   ├── checkpoint.py                    # DynamoDB get/put resume_token por collection
│   ├── batcher.py                       # Buffer por collection, flush a S3 JSONL.gz con gzip
│   ├── mappers.py                       # _cdc_envelope(doc, op, ts, deleted) + normalizers por coll (date→ISO, Decimal→float, datetime→ISO)
│   ├── listener.py                      # Core: motor.watch() per collection con resumeAfter, try/except, dlq on fail
│   ├── bootstrap.py                     # Full-sync initial load con __op='r' (solo primer arranque si DDB vacío)
│   └── observability.py                 # structlog JSON + CloudWatch EMF metrics (events/sec, lag, dlq_count)
└── tests/
    ├── test_mappers.py                  # Decimal, datetime UTC, None handling, discrepancy fields, resolved_fields map
    └── test_batcher.py                  # Size/time-based flush, gzip roundtrip
```

**Key decisions:**
- **pymongo sync** dentro de un `ThreadPoolExecutor` por colección (change_stream.next() es blocking; más simple que motor async con watch). 5 threads ≠ bottleneck.
- **Bootstrap idempotente**: si DDB no tiene `resume_token` para una colección, corre full snapshot primero con `__op=r`; al terminar graba `last_cluster_time` y empieza watch.
- **Backfill graceful**: si el listener se cae >24h (Mongo oplog retention Atlas M10=24h), reset con full snapshot + nuevo resume_token.
- **Idle flush**: si no hay eventos en 5 min, no se escribe archivo vacío (ahorra writes S3/autoloader overhead).

### 2. `infra/terraform/cdc.tf` — IaC

```hcl
# S3 bucket para eventos CDC (lifecycle: IA 30d, Glacier 90d, delete 365d)
resource "aws_s3_bucket" "cdc"
resource "aws_s3_bucket_lifecycle_configuration" "cdc"
resource "aws_s3_bucket_server_side_encryption_configuration" "cdc"
resource "aws_s3_bucket_public_access_block" "cdc"

# DynamoDB para resume tokens (on-demand PPR, <$1/mo)
resource "aws_dynamodb_table" "cdc_offsets" { billing_mode = "PAY_PER_REQUEST" }

# IAM role para el task (SG: egress only)
resource "aws_iam_role" "cdc_task"
resource "aws_iam_role_policy" "cdc_task"  # S3 PutObject en cdc/*, DDB GetItem/PutItem, Logs

# ECR repo para la imagen
resource "aws_ecr_repository" "cdc"

# CloudWatch log group (retention 14d)
resource "aws_cloudwatch_log_group" "cdc"

# ECS task definition + service (desired=0 hasta primer push, luego 1)
resource "aws_ecs_task_definition" "cdc"     # 256 CPU / 512 MB
resource "aws_ecs_service" "cdc"
variable "cdc_image_tag" { default = "bootstrap" }
variable "cdc_desired_count" { default = 0 }

# Security group: reutiliza worker_sg (egress-only) — ya existe
```

### 3. `nexus-medallion/src/bronze/` — Autoloader notebooks

5 notebooks espejo, uno por colección. Ejemplo `cdc_expenses.py`:

```python
import dlt
from pyspark.sql.types import StructType  # schemas explicitos importados

@dlt.table(
    name="mongodb_cdc_expenses",
    comment="CDC events via Autoloader desde s3://.../cdc/expenses/",
    table_properties={"delta.enableChangeDataFeed": "true", "quality": "bronze"},
)
def bronze_expenses_cdc():
    base = f"s3://{spark.conf.get('nexus.cdc_bucket')}/expenses/"
    schema_loc = f"s3://{spark.conf.get('nexus.cdc_bucket')}/_autoloader_state/expenses/"
    return (
        spark.readStream.format("cloudFiles")
          .option("cloudFiles.format", "json")
          .option("cloudFiles.schemaLocation", schema_loc)
          .option("cloudFiles.inferColumnTypes", "false")
          .option("cloudFiles.useIncrementalListing", "true")
          .schema(EXPENSES_CDC_SCHEMA)   # explícito: mismo schema que seed_bronze
          .load(base)
    )
```

### 4. `nexus-medallion/resources/pipelines/bronze_cdc.yml`

```yaml
resources:
  pipelines:
    bronze_cdc_pipeline:
      name: "nexus-bronze-cdc-${var.catalog}"
      catalog: ${var.catalog}
      target: "bronze"
      continuous: false          # triggered, se dispara cada N min vía job scheduler
      development: true
      serverless: true
      libraries:
        - notebook: { path: ../../src/bronze/cdc_expenses.py }
        - notebook: { path: ../../src/bronze/cdc_receipts.py }
        - notebook: { path: ../../src/bronze/cdc_hitl_tasks.py }
        - notebook: { path: ../../src/bronze/cdc_ocr_extractions.py }
        - notebook: { path: ../../src/bronze/cdc_expense_events.py }
      configuration:
        nexus.catalog: ${var.catalog}
        nexus.cdc_bucket: ${var.cdc_bucket}
```

Y `resources/jobs/bronze_cdc.yml` con trigger cron `*/5 * * * *` (cada 5 min) — también sirve para manual runs.

---

## Rollout / Cutover

El backend sigue escribiendo a Mongo igual. El cutover es invisible para el usuario:

1. **T-0**: Phase C quedó con `seed_bronze` corriendo manual + `silver/gold/vector` manual. bronze tables pobladas.
2. **Deploy CDC listener**:
   - `terraform apply -target=aws_s3_bucket.cdc -target=aws_dynamodb_table.cdc_offsets -target=aws_ecr_repository.cdc` (crea infra base)
   - `docker build + push` imagen `nexus-cdc:v1`
   - `terraform apply -var=cdc_image_tag=v1 -var=cdc_desired_count=1`
3. **Bootstrap**:
   - Listener detecta DDB vacío → corre full-sync → escribe snapshot a S3 con `__op=r` → graba resume_token + cluster_time
   - A partir de ahí, watch() empieza desde cluster_time
4. **Enable bronze_cdc pipeline**:
   - `databricks bundle deploy -t dev`
   - `databricks pipelines start --pipeline-id <bronze_cdc_pipeline>`
   - Primera ejecución: autoloader lee todos los archivos pre-existentes, merge schema, carga a bronze tables
5. **Switch silver/gold/vector a trigger automático** (opcional):
   - Crear job daily `*/5 * * * *` para bronze_cdc + silver + gold + vector.sync()
6. **Deprecate seed_bronze**:
   - Mover `seed_bronze_from_mongo.py` a `archive/` o marcar con comentario `# Deprecated by Phase B — solo para bootstrap de emergencia`
7. **Validación**:
   - Mongo: `db.expenses.updateOne({expense_id:'exp_X'}, {$set:{status:'approved'}})`
   - S3: aparece archivo en `cdc/expenses/.../batch_*.jsonl.gz` en <30s
   - Bronze: `SELECT * FROM bronze.mongodb_cdc_expenses WHERE expense_id='exp_X' ORDER BY __source_ts_ms DESC` — 2 filas (seed + update)
   - Silver: `SELECT status FROM silver.expenses WHERE expense_id='exp_X'` → 'approved'

---

## Cost estimate (dev)

| Recurso | Mensual |
|---|---|
| ECS Fargate task 256/512 24/7 | ~$7.50 |
| S3 storage (estimado 500 MB/mo) | ~$0.01 |
| S3 PUT requests (~50k/mo) | ~$0.25 |
| DynamoDB PPR (5 items × ~2k writes/mo) | <$0.05 |
| ECR storage (1 image × ~300 MB) | ~$0.03 |
| CloudWatch Logs (14d retention) | ~$0.50 |
| NAT egress (Mongo Atlas pull) | ~$3-5 |
| **Total** | **~$12/mo** |

---

## Riesgos / limitaciones conocidas

- **Mongo oplog retention Atlas M10**: ~24h. Si el listener está caído >24h, hay que resetear con full snapshot. Mitigación: CloudWatch alarma en `ECS service task count < 1` + auto-restart Fargate.
- **At-least-once delivery**: si crasheamos entre flush S3 y PutItem DDB, el próximo arranque reprocesa desde el último resume_token guardado → duplicados posibles en S3. Mitigación: silver `apply_changes` con `sequence_by=__source_ts_ms` es idempotente por construcción.
- **Orden por partición**: como no hay particionamiento (single task), el orden es global por colección → más fuerte que doc 04 (que promete orden por expense).
- **Schema drift**: si el backend añade un campo nuevo, Autoloader con schema explícito lo **ignorará**. Mitigación: añadir el campo a `EXPENSES_CDC_SCHEMA` en el notebook + redeploy pipeline. Proceso manual pero explícito.
- **Autoloader `_autoloader_state/`**: no borrar nunca este prefijo, o pipeline reprocesa todo desde cero.

---

## Path a producción (futuro)

Cuando hack-grade no alcance y haya presupuesto:

1. **Swap listener → Debezium Server** (mismo container pattern, config JSON, output a Kinesis)
2. **Swap S3 → MSK Serverless** (Databricks tiene conector Kafka nativo)
3. **Añadir Schema Registry** (Glue Schema Registry, gratis hasta 1M ops/mo)
4. **Multi-consumer**: pub/sub de alerts de fraude, warehouse BI, etc.

Bronze tables y silver/gold/vector notebooks NO cambian.
