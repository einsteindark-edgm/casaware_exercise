# Phase B · Postmortem & Estudio — CDC Mongo → Bronze

> **Fecha**: 2026-04-24
> **Scope**: Reemplazar el seed manual `seed_bronze_from_mongo.py` por un pipeline CDC continuo que mantenga el medallion vivo sin intervención.
> **Costo target**: < $20/mo en AWS (vs ~$200/mo de MSK+Debezium full-prod).
> **Status final**: ✅ desplegado y verificado end-to-end.

---

## 0. Contexto previo (Phase A + Phase C)

Antes de tocar Phase B, el stack tenía:

- **Phase A** (AWS): ECS cluster con backend/frontend/worker en Fargate, ALB, RDS-free via MongoDB Atlas, ElastiCache Redis, Cognito JWT, Temporal via ngrok → ECS, receipts/textract S3.
- **Phase C** (Databricks): Unity Catalog `nexus_dev` con 5 schemas, storage credential cross-account a bucket `nexus-dev-edgm-uc`, pipeline `silver` (SCD1 apply_changes), `gold` (expense_audit), notebook `vector_setup` creando índice Vector Search sobre `gold.expense_chunks`.

Lo que **faltaba**: las tablas `bronze.mongodb_cdc_*` se poblaban corriendo manualmente el notebook `seed_bronze_from_mongo.py`. Cualquier cambio en Mongo (nuevo expense, HITL resolve, OCR extract) solo aparecía en silver/gold **después** de correr el seed a mano.

---

## 1. Decisión arquitectónica: "Debezium-lite"

El doc `04-cdc-ingestion.md` propone el stack canónico: **Debezium Server + Kafka + Kafka Connect S3 Sink**. En producción real es lo correcto (multi-consumer, replay arbitrario por offset, schema registry Avro).

**Problema**: en dev esto cuesta ~$200/mo solo de MSK, más ops overhead (operar Kafka Connect, Schema Registry, etc.). Para un entorno que hoy tiene ~10 expenses de prueba, es excesivo.

**Solución adoptada** — "Debezium-lite":

```
MongoDB Atlas
    │  pymongo.watch() por cada colección
    ▼
nexus-cdc (ECS Fargate, 1 task Python)
    │  - resume_token por colección → DynamoDB (PPR)
    │  - buffering: 200 eventos o 30s → flush
    │  - crashes → DLQ en S3
    ▼
s3://nexus-dev-edgm-cdc/{coll}/year=.../batch_<ULID>.jsonl.gz
    │  (JSONL comprimido, Hive-style partitioning)
    ▼
Databricks Autoloader (cloudFiles JSON, schema explícito)
    │
    ▼
bronze.mongodb_cdc_*  (ya existía: silver/gold/vector consumen sin cambios)
```

### Lo crítico: el envelope JSON es **idéntico** al de Debezium

Debezium con `ExtractNewDocumentState + add.fields: op,source.ts_ms` produce:

```json
{ <campos del doc>, "__op": "c", "__source_ts_ms": 1745331000000, "__deleted": false }
```

El listener emite exactamente eso. Significado: el día que migremos a Debezium real, cambia **solo el productor**. Bronze/silver/gold no se tocan.

### Tabla comparativa (para tu referencia de estudio)

| Aspecto | Debezium+MSK (doc 04) | Debezium-lite (esta implementación) |
|---|---|---|
| Costo mensual | ~$200 | ~$12 |
| Latencia Mongo→bronze | ~3s | ~35s (30s batcher + 5s autoloader) |
| Replay arbitrario | Sí, a cualquier offset | No — desde último resume_token DDB |
| Multi-consumer | Sí | No (solo bronze lee) |
| Ordering | Por partición Kafka | Global por colección (single task) |
| Schema registry | Avro + Confluent SR | Explícito en notebooks |
| Delivery | Al menos una vez | Al menos una vez |
| Idempotencia en silver | `apply_changes` + `sequence_by` | Igual |

---

## 2. Anatomía del listener (`nexus-cdc/`)

### 2.1. Flujo de un evento Mongo → S3

```
1. pymongo.Collection.watch(pipeline=[], full_document='updateLookup')
   ├─ Si DDB tiene resume_token → watch(resume_after=token)
   ├─ Si DDB vacío + bootstrap flag → full_sync primero (emit __op=r)
   │                                    ↓
   │                               col.find({}) → envelope_from_snapshot(doc)
   │                                    ↓
   │                               batcher.add(event)
   │                                    ↓
   │                               batcher.flush(reason='bootstrap_end')
   │                                    ↓
   │                               offsets.mark_bootstrap_complete(col, cluster_time)
   │
   └─ En watch loop:
      for change in stream:
          envelope = envelope_from_change(change)  # mapping insert→c / update→u / delete→d
          batcher.add(envelope)                    # buffer in-memory
          if buffer.size >= 200:
              flush to S3 (gzip JSONL)
              offsets.put(resume_token, last_ts_ms)
```

### 2.2. Archivos clave y qué hacen

```
nexus-cdc/src/nexus_cdc/
├── config.py         → Settings: MONGODB_URI, S3_BUCKET, DDB_OFFSETS_TABLE, batch params
├── mappers.py        → _json_safe: Decimal→float, datetime→ISO, ObjectId→str, drop _id
│                       envelope_from_snapshot(doc): para bootstrap (__op=r)
│                       envelope_from_change(change): para watch (__op=c|u|d)
├── checkpoint.py     → OffsetStore(DynamoDB): get/put resume_token + last_ts_ms
├── batcher.py        → S3Batcher: buffer eventos, flush por size (200) o tiempo (30s),
│                       DLQ a s3://.../dlq/ si un evento falla
├── listener.py       → CollectionListener: owns pymongo.watch() + bootstrap + retry
└── main.py           → Entry point: 1 thread por colección + tick thread para flush time-based
```

### 2.3. Decisiones de diseño importantes

**pymongo sync + threads, no motor async.** Motor (async pymongo) sería más "moderno" pero `change_stream.watch()` es blocking y el control flow se complica. 5 colecciones × 1 thread = 5 threads es trivial para Fargate 256 CPU.

**Resume token en DynamoDB, no en S3.** DDB PPR cuesta <$1/mo para esta escala. Ventaja vs S3: atomic update con consistency fuerte. Si crasheamos entre flush S3 y update DDB → at-least-once → silver maneja duplicados con `sequence_by`.

**Bootstrap idempotente.** Si DDB no tiene `bootstrap_completed_at` para una colección, el listener corre `col.find({})` emit-all con `__op=r`, guarda cluster_time, y recién ahí entra al watch loop con `start_at_operation_time=cluster_time`. Esto cubre:
- Primer deploy (empty DDB)
- Mongo oplog expiró (>24h en Atlas M10) — hay que dropear offset y reiniciar el task, el bootstrap re-captura todo

**Idle flush.** Si pasa `IDLE_FLUSH_SECONDS=300` sin eventos, **NO** se escribe un archivo vacío. Evita ballooning de metadata en Autoloader.

**DLQ.** Si el mapper crashea en un doc (ej. un campo BSON exótico), el evento va a `s3://.../dlq/expenses/year=.../evt_<ULID>.json` y se continúa. Nunca se detiene el listener por 1 evento malo.

---

## 3. Autoloader en Databricks (`src/bronze/cdc_*.py`)

5 notebooks espejo. Patrón único:

```python
@dlt.table(
    name="mongodb_cdc_expenses",
    table_properties={"delta.enableChangeDataFeed": "true", "quality": "bronze"},
)
def mongodb_cdc_expenses():
    base = f"s3://{spark.conf.get('nexus.cdc_bucket')}/expenses/"
    schema_loc = f"s3://{spark.conf.get('nexus.cdc_bucket')}/_autoloader_state/expenses/"
    return (
        spark.readStream.format("cloudFiles")
        .option("cloudFiles.format", "json")
        .option("cloudFiles.schemaLocation", schema_loc)      # checkpoint de Autoloader
        .option("cloudFiles.inferColumnTypes", "false")       # no inferir — schema explícito
        .schema(EXPENSES_JSON_SCHEMA)                         # DoubleType/StringType (JSON nativo)
        .load(base)
        .select(
            # cast explícito a DecimalType/TimestampType — coincide con seed
            col("amount").cast("decimal(18,2)"),
            col("created_at").cast("timestamp"),
            ...
        )
    )
```

### Por qué schema explícito (no inference)

El CDC emitter hace `Decimal → float` (JSON-safe) y `datetime → ISO string`. Si dejamos Autoloader inferir, termina declarando `amount` como `string` cuando ve "100.0" vs `double` cuando ve 100.5. Con schema explícito evitamos drift entre archivos.

El cast a `decimal(18,2)` y `timestamp` en el select asegura que el schema final de bronze sea idéntico al que producía `seed_bronze_from_mongo.py`. Silver no nota la diferencia.

### Por qué `_autoloader_state/` es sagrado

Autoloader usa ese prefijo para trackear qué archivos ya procesó. Si lo borrás, reprocesa todo el bucket desde cero → duplicados en bronze (los maneja silver con `sequence_by`, pero es desperdicio). Por eso el lifecycle de S3 tiene una regla específica que excluye ese prefijo de cualquier expiración.

---

## 4. El pipeline completo (lakehouse)

```
nexus-cdc-refresh (job cron */5 min)
    ├─ task: bronze_cdc_pipeline (5 flows en paralelo)
    ├─ task: silver_pipeline (4 flows, depende de bronze)
    └─ task: gold_pipeline (1 flow, depende de silver)
```

El vector_setup es manual (el sync de Vector Search es lento, ~5min, no vale la pena cada 5 min).

### Arquivos yaml importantes

- `nexus-medallion/resources/pipelines/bronze_cdc.yml` — pipeline con las 5 notebooks, serverless+triggered
- `nexus-medallion/resources/jobs/cdc_refresh.yml` — cron Quartz `0 */5 * * * ?` UTC
- `nexus-medallion/databricks.yml` — variable `cdc_bucket` añadida

---

## 5. Infra Terraform (`infra/terraform/cdc.tf`)

13 recursos nuevos:

| Recurso | Propósito |
|---|---|
| `aws_ecr_repository.cdc` | Image registry, IMMUTABLE tags, scan on push |
| `aws_s3_bucket.cdc` | Landing de JSONL.gz |
| `aws_s3_bucket_lifecycle_configuration.cdc` | 30d→IA, 90d delete; DLQ 365d; _autoloader_state 3650d |
| `aws_s3_bucket_server_side_encryption_configuration.cdc` | AES256 |
| `aws_s3_bucket_public_access_block.cdc` | Bloquea público total |
| `aws_dynamodb_table.cdc_offsets` | Resume tokens, PPR billing |
| `aws_iam_role.cdc_task` | Task role Fargate |
| `aws_iam_role_policy.cdc_task` | S3 Put/Get + DDB Get/Put/Update |
| `aws_cloudwatch_log_group.cdc` | 14d retention |
| `aws_ecs_task_definition.cdc` | 256 CPU / 512 MB, MONGODB_URI from Secrets Manager |
| `aws_ecs_service.cdc` | desired=1, SG=worker (egress-only, reutiliza) |
| `aws_ssm_parameter.cdc_bucket` | Exposed for medallion team |
| `aws_ssm_parameter.cdc_offsets_table` | Idem |

### Extensiones a recursos existentes

- `databricks.tf`: añadido `S3CDCReadWrite` statement al policy del UC role + nuevo `databricks_external_location.cdc` sobre el bucket CDC.

---

## 6. Bootstrap journey (qué pasó paso a paso en el deploy real)

### T+0: `terraform apply` targeted

```bash
terraform apply -target=aws_ecr_repository.cdc \
  -target=aws_s3_bucket.cdc \
  -target=aws_s3_bucket_lifecycle_configuration.cdc \
  -target=aws_s3_bucket_public_access_block.cdc \
  -target=aws_s3_bucket_server_side_encryption_configuration.cdc \
  -target=aws_dynamodb_table.cdc_offsets \
  -target=aws_iam_role.cdc_task \
  -target=aws_iam_role_policy.cdc_task \
  -target=aws_cloudwatch_log_group.cdc \
  -target=aws_ssm_parameter.cdc_bucket \
  -target=aws_ssm_parameter.cdc_offsets_table \
  -target=aws_ecr_lifecycle_policy.cdc
# → 12 added, 0 changed, 0 destroyed
```

Usé `-target` para evitar tocar task defs de backend/frontend/worker (que habrían sido replaced por cambios en el execution role policy).

### T+2min: build + push imagen

```bash
docker build --platform linux/amd64 --provenance=false -t <repo>:v1 .
docker push <repo>:v1a
```

**Gotcha #1**: el primer push falló porque Docker BuildKit crea un **manifest list multi-arch** por default, y ECR rechaza con 400 en repos IMMUTABLE que ya vieron layers. Arreglo: `--provenance=false` + retag a `v1a`.

### T+5min: deploy task + scale service

```bash
terraform apply \
  -target=aws_ecs_task_definition.cdc \
  -target=aws_ecs_service.cdc \
  -var=cdc_image_tag=v1a \
  -var=cdc_desired_count=1
```

### T+7min: task RUNNING, bootstrap ejecutado

Logs del listener (los primeros segundos):

```json
{"event":"startup","collections":["expenses","receipts","hitl_tasks","ocr_extractions","expense_events"]}
{"event":"mongo_ping_ok"}
{"event":"bootstrap_start","collection":"expenses"}
{"event":"batch_flushed","collection":"expenses","events":4,"bytes":655,"reason":"bootstrap_end"}
{"event":"bootstrap_snapshot","collection":"expenses","docs":4}
{"event":"bootstrap_complete","collection":"expenses","cluster_time_ms":1776998107134}
{"event":"watch_start","collection":"expenses","start_at_ms":1776998107134}
```

5 archivos JSONL.gz aparecen en S3 en ~2 segundos (bootstrap fue rápido porque Mongo tiene pocos docs).

### T+10min: external location + DLT deploy

```bash
# UC policy extendido + external location creada
terraform apply -target=aws_iam_role_policy.uc_access -target=databricks_external_location.cdc

# Pipeline deployado
cd nexus-medallion && databricks bundle deploy -t dev
```

**Gotcha #2**: la primera corrida del pipeline falló con `MANAGED table already exists`. El seed anterior había creado `bronze.mongodb_cdc_expenses` como managed SQL table. DLT no puede "adoptar" tablas existentes — necesita crearlas él mismo.

### T+15min: cutover (DROP tablas viejas)

5 × `DROP TABLE IF EXISTS nexus_dev.bronze.mongodb_cdc_*` via SQL Warehouse. Sin pérdida de datos porque el bootstrap del listener ya había emitido el mismo snapshot a S3.

### T+17min: pipeline re-run

```
bronze_cdc_pipeline: 5/5 flows COMPLETED (~14 segundos total)
silver_pipeline (full-refresh-all): 4/4 flows COMPLETED
gold_pipeline (full-refresh-all): 1/1 flow COMPLETED
```

**Gotcha #3**: silver falló la primera vez con `DIFFERENT_DELTA_TABLE_READ_BY_STREAMING_SOURCE`. Su checkpoint apuntaba al table_id vieja (dropeada). Solución: `--full-refresh-all`.

### Final counts

```
bronze.expenses   → 4
bronze.receipts   → 4
bronze.hitl       → 1
bronze.ocr        → 3
bronze.events     → 18
silver.expenses   → 4  (apply_changes dedup)
silver.ocr        → 3
silver.hitl       → 1
silver.events     → 18
gold.audit        → 3  (solo status='approved')
```

---

## 7. Contratos preservados (lo más importante para entender)

**Clave**: nada más allá de `bronze/` cambió. El secreto es que el envelope que escribe el listener es byte-por-byte compatible con lo que escribía el seed:

```json
{
  "expense_id": "exp_...",
  "tenant_id": "t_alpha",
  "amount": 100.5,
  "status": "approved",
  ...
  "__op": "r",                  // r=snapshot, c=create, u=update, d=delete
  "__source_ts_ms": 1776998107134,
  "__deleted": false
}
```

Silver hace esto sobre bronze:

```python
dlt.apply_changes(
    target="expenses",
    source="v_expenses_cdc_cleansed",
    keys=["expense_id"],
    sequence_by=col("_source_ts_ms"),    # ← clave para idempotencia
    apply_as_deletes=expr("_op = 'd'"),
    stored_as_scd_type="1",
)
```

`sequence_by=__source_ts_ms` significa: si por at-least-once delivery llegan 2 veces el mismo evento, silver queda con el más reciente (por timestamp), efectivamente idempotente. Por eso podemos tolerar duplicados sin corromper silver/gold.

---

## 8. Costos reales

| Recurso | $/mo estimado |
|---|---|
| ECS Fargate 256/512 × 730h | ~$7.50 |
| S3 (storage + PUTs) | ~$0.30 |
| DynamoDB PPR | ~$0.05 |
| CloudWatch Logs 14d | ~$0.50 |
| ECR storage | ~$0.03 |
| NAT egress Mongo | ~$3-5 |
| **Total** | **~$11-13** |

Contrasta con MSK Serverless (~$150) + Kafka Connect ECS (~$30) + Schema Registry Glue (~$5) = **$185/mo** para el stack full Debezium. 95% ahorro con el mismo contrato semántico.

---

## 9. Runbooks (guardarlos para cuando rompa)

### 9.1. Listener caído > 24h (oplog expirado)

Síntoma: logs del listener repiten `mongo_error ChangeStreamHistoryLost`.

```bash
# 1. Scale down
aws ecs update-service --cluster nexus-dev-edgm --service nexus-dev-edgm-cdc --desired-count 0

# 2. Clear DDB offsets para forzar bootstrap
for coll in expenses receipts hitl_tasks ocr_extractions expense_events; do
  aws dynamodb delete-item \
    --table-name nexus-dev-edgm-cdc-offsets \
    --key "{\"collection\":{\"S\":\"$coll\"}}"
done

# 3. Scale up (auto-bootstrap)
aws ecs update-service --cluster nexus-dev-edgm --service nexus-dev-edgm-cdc --desired-count 1
```

Bronze tables NO se borran — el `apply_changes` en silver deduplica por `__source_ts_ms`.

### 9.2. Bronze corrupta / drift de schema

```bash
# 1. Stop listener
aws ecs update-service --cluster nexus-dev-edgm --service nexus-dev-edgm-cdc --desired-count 0

# 2. Drop bronze tables + autoloader state
for t in expenses receipts hitl_tasks ocr_extractions expense_events; do
  databricks api post /api/2.0/sql/statements -p nexus-dev --json "{
    \"statement\": \"DROP TABLE IF EXISTS nexus_dev.bronze.mongodb_cdc_$t\",
    \"warehouse_id\": \"807ffe706cfba0e0\", \"wait_timeout\": \"30s\"
  }"
done
aws s3 rm s3://nexus-dev-edgm-cdc/_autoloader_state/ --recursive

# 3. Clear offsets
aws dynamodb delete-table --table-name nexus-dev-edgm-cdc-offsets
terraform apply -target=aws_dynamodb_table.cdc_offsets

# 4. Re-start
aws ecs update-service --cluster nexus-dev-edgm --service nexus-dev-edgm-cdc --desired-count 1

# 5. Re-run pipeline (full refresh)
databricks bundle run bronze_cdc_pipeline --full-refresh-all -t dev
databricks bundle run silver_pipeline --full-refresh-all -t dev
databricks bundle run gold_pipeline --full-refresh-all -t dev
```

### 9.3. Inspeccionar DLQ

```bash
aws s3 ls s3://nexus-dev-edgm-cdc/dlq/ --recursive
aws s3 cp s3://nexus-dev-edgm-cdc/dlq/expenses/.../evt_XYZ.json -
```

### 9.4. Lag end-to-end

```sql
SELECT
  'expenses' AS collection,
  MAX(__source_ts_ms) AS latest_event_ms,
  (unix_millis(current_timestamp()) - MAX(__source_ts_ms))/1000 AS lag_seconds
FROM nexus_dev.bronze.mongodb_cdc_expenses;
```

Esperado: lag_seconds típicamente 30-60s en horas activas.

---

## 10. Qué estudiar para entender esto a fondo

### Conceptos core
1. **Change Data Capture**: https://www.confluent.io/learn/change-data-capture/
2. **MongoDB Change Streams**: https://www.mongodb.com/docs/manual/changeStreams/
3. **Debezium architecture**: https://debezium.io/documentation/reference/stable/architecture.html
4. **Kafka Connect**: https://docs.confluent.io/platform/current/connect/index.html (para entender qué emulamos)

### Databricks
1. **Delta Live Tables / Lakeflow**: https://docs.databricks.com/aws/en/dlt/
2. **Autoloader**: https://docs.databricks.com/aws/en/ingestion/cloud-object-storage/auto-loader/
3. **SCD Type 1 con apply_changes**: https://docs.databricks.com/aws/en/dlt/cdc
4. **Unity Catalog external locations**: https://docs.databricks.com/aws/en/connect/unity-catalog/cloud-storage/external-locations

### AWS
1. **ECS Fargate**: por qué 1 task 256/512 es suficiente aquí vs lambdas
2. **DynamoDB PPR vs provisioned**: para workloads small+sporadic PPR gana
3. **IAM cross-account trust**: cómo UC Master assume nuestro role via External ID

### Patterns que aplicamos
1. **At-least-once + idempotent consumer**: el paper *Reliability in Distributed Systems* (Jim Gray) cubre esto bien. La idea: el productor puede duplicar, el consumidor garantiza idempotencia. Aquí: listener (producer) + silver `apply_changes sequence_by` (idempotent consumer).
2. **Outbox pattern alternative**: CDC directo es más simple que outbox cuando controlas el schema del origen.
3. **Schema on read vs schema on write**: escogimos schema on **write** (explícito en notebooks) para evitar drift, tradeoff es que añadir campo → cambiar notebook.

---

## 11. Diferencias entre lo que hicimos y el "doc 04" canónico

Lo que el doc pide que **no** implementamos (intencional):

| Feature | Doc 04 | Esta implementación | Impacto |
|---|---|---|---|
| Kafka broker | Requerido | No | No hay multi-consumer. Agregar después = swap productor |
| Schema Registry | Confluent SR + Avro | Schema explícito en Python | Drift se detecta en runtime, no compile-time |
| Kafka Connect | Debezium en Connect | Python script custom | Menos features (SMTs, converters) pero cubre el 80% |
| S3 Sink Connector (DR backup) | Paralelo | No | El S3 bucket YA es el primary, no hay diff |
| DLQ topic | Kafka `nexus.dlq` | S3 prefix `dlq/` | Menos inspectable, pero sirve igual |
| Pre/post images | Requerido | `fullDocument: updateLookup` | Equivalente — el doc completo viene en updates |
| Capture mode | `change_streams_update_full_with_pre_image` | `updateLookup` | Sin pre-image, pero silver no lo necesita |

Lo que **sí** hicimos que el doc no dice:
- Bootstrap full-sync idempotente (el doc asume snapshot de Debezium)
- Idle flush (ahorra archivos vacíos)
- DLQ en S3 (el doc asume Kafka DLQ)

---

## 12. Checklist de qué aprendiste después de leer esto

- [ ] Por qué CDC > batch seed para un medallion vivo
- [ ] Cómo MongoDB Change Streams funcionan (oplog, resume tokens, cluster time)
- [ ] Por qué el envelope con `__op/__source_ts_ms/__deleted` es el contrato universal CDC
- [ ] Cómo Autoloader difiere de DLT regular (`cloudFiles` vs `dlt.read`)
- [ ] Por qué schema explícito > inferencia para datos de producción
- [ ] Cómo SCD Type 1 con `sequence_by` garantiza idempotencia
- [ ] Trade-offs de Debezium+Kafka vs custom listener para dev/scale
- [ ] Qué es un UC external location y por qué UC role necesita S3 policies específicos
- [ ] Por qué `_autoloader_state/` es sagrado (no borrar nunca)
- [ ] El runbook de emergency re-bootstrap

---

## 13. Archivos creados/modificados (referencia rápida)

```
CREADOS:
├── infra/plans/phase-b.md                           # Plan de diseño
├── infra/plans/phase-b-postmortem.md                # Este documento
├── infra/terraform/cdc.tf                           # 13 recursos AWS
├── nexus-cdc/                                       # Paquete Python completo
│   ├── pyproject.toml
│   ├── Dockerfile
│   ├── README.md
│   ├── .env.example
│   ├── src/nexus_cdc/
│   │   ├── __init__.py
│   │   ├── config.py
│   │   ├── main.py
│   │   ├── listener.py
│   │   ├── batcher.py
│   │   ├── checkpoint.py
│   │   ├── mappers.py
│   │   └── observability.py
│   └── tests/
│       ├── test_mappers.py      (9 tests)
│       └── test_batcher.py      (4 tests)
├── nexus-medallion/src/bronze/
│   ├── cdc_expenses.py
│   ├── cdc_receipts.py
│   ├── cdc_hitl_tasks.py
│   ├── cdc_ocr_extractions.py
│   └── cdc_expense_events.py
├── nexus-medallion/resources/pipelines/bronze_cdc.yml
└── nexus-medallion/resources/jobs/cdc_refresh.yml   # cron */5 min

MODIFICADOS:
├── infra/terraform/databricks.tf                    # +S3CDCReadWrite stmt, +external_location.cdc
├── nexus-medallion/databricks.yml                   # +var cdc_bucket
└── nexus-medallion/src/seed/seed_bronze_from_mongo.py  # marcado DEPRECATED
```

---

## 14. Próximos pasos lógicos

- **Phase B+** (opcional, cuando volumen > 100 msg/s):
  - Swap listener.py → Debezium Server container (~200 LoC Java config)
  - Añadir MSK Serverless cluster (~$150/mo)
  - Databricks conector Kafka nativo (reemplaza Autoloader S3)
  - **Bronze/silver/gold NO cambian** — mismo envelope
- **Alertas operativas**: CloudWatch alarm en `log_filter pattern="listener_crashed"` → SNS.
- **Dashboard Grafana** leyendo métricas de CW EMF (events/sec, DLQ count, lag).
- **Delete de chat_sessions/chat_turns**: si usás el chatbot RAG, añadir esas 2 colecciones al listener (1 línea en `config.py DEFAULT_COLLECTIONS`).

---

**Fin del postmortem.** Cualquier duda, empezá por leer `phase-b.md` (decisiones) y después este (ejecución). El código es pequeño — 400 LoC de Python + 200 LoC de Terraform + 5 notebooks Databricks cortos. Todo es lecturable en un asiento.
