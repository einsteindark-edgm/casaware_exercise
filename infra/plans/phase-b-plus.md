# Phase B+ · Evolución a Debezium + Kafka (MSK) — documento de referencia

> **Status**: Documentado, NO implementado.
> **Trigger**: Se ejecuta cuando el tráfico/requerimientos superen lo que Phase B dev-tier puede servir. Ver §3 "Cuándo migrar".
> **Relación**: Es la implementación canónica del doc `04-cdc-ingestion.md` original. Phase B fue la versión dev-tier que decidimos construir primero por costo (~$13/mo vs ~$185/mo).

---

## 1. Qué tenemos HOY (Phase B dev-tier)

### 1.1. Arquitectura actual

```
┌─────────────────┐
│ MongoDB Atlas   │      Change Streams API (oplog)
│  (Replica Set)  │──────────────────┐
└─────────────────┘                  │
                                     ▼
                    ┌────────────────────────────────┐
                    │ nexus-cdc (ECS Fargate 1 task) │
                    │  - Python con pymongo.watch()  │
                    │  - 1 thread por colección (5)  │
                    │  - Buffering in-memory (200    │
                    │    eventos o 30s → flush)      │
                    │  - DLQ local en S3             │
                    └──────┬─────────────┬───────────┘
                           │             │
                           ▼             ▼
         ┌──────────────────────┐  ┌──────────────────┐
         │ S3: jsonl.gz gzipped │  │ DynamoDB: resume │
         │ (Hive partitioned)   │  │ tokens por coll  │
         └──────────┬───────────┘  └──────────────────┘
                    │
                    ▼
         ┌────────────────────────────────┐
         │ Databricks Autoloader          │
         │ (cloudFiles JSON, schema       │
         │ explícito, triggered cada 5m)  │
         └──────────┬─────────────────────┘
                    │
                    ▼
         ┌──────────────────────────────────┐
         │ bronze.mongodb_cdc_*  (5 tablas) │
         └──────────┬───────────────────────┘
                    │
                    ▼                       silver → gold → vector_search
         ┌─────────────────────┐
         │ silver apply_changes│
         │ SCD1 sequence_by    │
         │ __source_ts_ms      │
         └─────────────────────┘
```

### 1.2. Cómo funciona hoy paso a paso

1. **Captura**: `nexus-cdc` tiene un thread por colección Mongo. Cada thread abre un `change_stream` con `full_document=updateLookup` para recibir el doc completo en updates. Mongo envía cada cambio (insert/update/delete) en ~200ms.

2. **Normalización** (`mappers.py`): BSON → JSON-safe. `Decimal → float`, `datetime → ISO string`, `ObjectId → str`, drop `_id` interno. Se añade el envelope `{__op: 'c'|'u'|'d'|'r', __source_ts_ms: int, __deleted: bool}`.

3. **Buffering** (`batcher.py`): eventos acumulan en memoria. Flush dispara por:
   - **Size**: 200 eventos acumulados
   - **Time**: 30s desde el primer evento del buffer
   - **Shutdown**: SIGTERM limpio

4. **Persistencia de offset** (`checkpoint.py`): después de cada flush exitoso a S3, `resume_token` (blob opaco de Mongo) se escribe en DynamoDB con `last_ts_ms`. Si el task crashea, el próximo arranque lee el último token y reanuda desde ahí — **at-least-once delivery**.

5. **Sink**: JSONL gzipped se escribe a `s3://nexus-dev-edgm-cdc/{coll}/year=YYYY/month=MM/day=DD/hour=HH/batch_<ULID>.jsonl.gz`. El ULID en el nombre garantiza orden lexicográfico.

6. **Ingesta a bronze**: Databricks Autoloader (`cloudFiles`) hace `ls` incremental del bucket cada vez que corre, detecta archivos nuevos, los parsea con schema explícito, y appendea a `bronze.mongodb_cdc_*`. Checkpoint propio en `s3://.../_autoloader_state/`.

7. **Merge en silver**: `dlt.apply_changes(keys=..., sequence_by=__source_ts_ms)` hace SCD1 — una fila por business key, siempre con el valor más reciente por timestamp. Esto neutraliza duplicados del at-least-once.

### 1.3. Qué limitaciones tiene

| Limitación | Causa técnica | Impacto hoy |
|---|---|---|
| Latencia E2E ~35s | Batcher 30s + Autoloader trigger cada 5min (actualmente) | Invisible con ~10 eventos/día |
| Single-point-of-failure | 1 ECS task, si cae, 0 throughput | `desired_count=1` sin auto-restart agresivo |
| Replay desde offset arbitrario | Solo guardamos último `resume_token` | No podés rebobinar a "ayer 14:00" sin full re-bootstrap |
| Sin multi-consumer | Solo bronze lee S3 | No hay warehouse BI ni feature store separados |
| Sin schema validation | Schema en Python, no en contrato | Campo nuevo backend → notebook lo ignora silently |
| Throughput ceiling | 1 task 256 CPU, sync Python + PUT S3 | ~30-50 events/s sostenido antes de lag |
| Alertas mínimas | Solo CloudWatch logs | Nadie se entera si un thread crashea silenciosamente |

**Ninguna** de estas limitaciones duele con el volumen actual. Por eso elegimos Phase B. Cuando dolean, hay que pasar a Phase B+.

---

## 2. Qué propone Phase B+

### 2.1. Arquitectura objetivo

```
┌─────────────────┐
│ MongoDB Atlas   │      Change Streams + pre-image habilitado
│  (Replica Set)  │──────────────────┐
│                 │                  │
│ pre/post images │◀── requiere collMod en cada coll
└─────────────────┘                  │
                                     ▼
                    ┌────────────────────────────────┐
                    │ Debezium Server (ECS Fargate)  │
                    │  - Java agent oficial          │
                    │  - capture.mode=update_full    │
                    │    _with_pre_image             │
                    │  - SMT ExtractNewDocumentState │
                    │  - Avro + Schema Registry      │
                    └──────┬─────────────────────────┘
                           │
                           ▼
                    ┌──────────────────────────────┐
                    │ MSK Serverless (Kafka 3.7+)  │
                    │  Topics:                     │
                    │   nexus.nexus_dev.expenses   │
                    │   nexus.nexus_dev.receipts   │
                    │   nexus.nexus_dev.hitl_tasks │
                    │   nexus.nexus_dev.ocr_...    │
                    │   nexus.nexus_dev.events     │
                    │   nexus.dlq                  │
                    │                              │
                    │ Retention: 7d, partitions: 6 │
                    │ Compression: zstd            │
                    └──┬──────────────┬─────────┬──┘
                       │              │         │
          ┌────────────┘              │         └────────────┐
          ▼                           ▼                      ▼
 ┌──────────────────┐   ┌─────────────────────┐  ┌────────────────┐
 │ Databricks Kafka │   │ S3 Sink Connector   │  │ Otros consumers│
 │ Source (bronze)  │   │ (DR backup, parquet)│  │ - Warehouse BI │
 │ Structured Stream│   │                     │  │ - Feature Store│
 └────────┬─────────┘   └─────────────────────┘  │ - Fraud alerts │
          │                                      └────────────────┘
          ▼
 ┌──────────────────────────────────┐
 │ bronze.mongodb_cdc_*  (MISMAS!)  │
 └──────────┬───────────────────────┘
            │
            ▼                              silver → gold → vector_search
 ┌─────────────────────┐
 │ silver apply_changes│
 │ SCD1 sequence_by    │   ← sin cambios
 │ __source_ts_ms      │
 └─────────────────────┘
```

### 2.2. Qué mejora, concretamente

| Dimensión | Phase B (hoy) | Phase B+ | Factor de mejora |
|---|---|---|---|
| Latencia E2E Mongo→bronze | ~35s | ~3s | **~12×** |
| Throughput sostenido | ~30-50 msg/s | 10,000+ msg/s | **200×** |
| Replay a offset arbitrario | No | Sí, a cualquier offset de los últimos 7d | N/A — capability nueva |
| Multi-consumer | 1 (solo bronze) | N (warehouse, ML, alerts, etc.) | N/A — capability nueva |
| HA de la ingesta | 1 task, restart automático ECS | Kafka Connect HA cluster + Debezium auto-failover | N/A — 99.9% → 99.99% |
| Schema evolution | Manual (editar notebook) | Avro con compatibility rules + Schema Registry | N/A — safer |
| DLQ | S3 prefix, inspección manual | Topic Kafka con tooling (lag, consumer groups) | Mejor DX |
| Auditoría | S3 bucket versioned | Kafka retention + offset inspection + ACLs | Mejor compliance |
| Backup DR | S3 es el primary | Kafka primary + S3 Sink paralelo | Redundancia real |

### 2.3. Lo CRÍTICO que NO cambia

Este es el punto de haber diseñado Phase B como "Debezium-lite":

- **Envelope del evento**: `{ <fields>, __op, __source_ts_ms, __deleted }` — idéntico byte-a-byte
- **Bronze tables**: mismos nombres, mismo schema, mismas propiedades Delta
- **Silver pipeline**: apply_changes con `sequence_by=__source_ts_ms` funciona igual
- **Gold + Vector Search**: intactos
- **Contratos de backend → Mongo**: intactos

Esto significa que la migración es un **swap del productor**, no un rewrite. Todo el medallion queda estable durante el cutover.

### 2.4. Mapping de componentes Phase B → Phase B+

| Componente Phase B | Reemplazado por | Cambio de código |
|---|---|---|
| `nexus-cdc/src/nexus_cdc/listener.py` | Debezium Server MongoDB connector | Elimina 100 LoC Python, añade 30 líneas JSON config |
| `nexus-cdc/src/nexus_cdc/mappers.py` | Debezium SMT `ExtractNewDocumentState` + `add.fields` | Elimina 80 LoC, config declarativo |
| `nexus-cdc/src/nexus_cdc/batcher.py` | Kafka producer interno de Debezium | Elimina 120 LoC |
| `nexus-cdc/src/nexus_cdc/checkpoint.py` | `__consumer_offsets` topic de Kafka | Elimina 50 LoC |
| `s3://.../cdc/` bucket | MSK topics (opcional: S3 Sink paralelo) | Se mantiene como DR backup |
| `aws_dynamodb_table.cdc_offsets` | Kafka offsets internal | Se elimina |
| `src/bronze/cdc_*.py` (Autoloader) | 1 notebook con Kafka source | Reemplaza 5 notebooks por 1 |
| `resources/pipelines/bronze_cdc.yml` | Igual, pero apunta a 1 notebook | Update paths |
| `aws_ecs_service.cdc` (Python) | `aws_ecs_service.debezium` (Java container) | Nuevo service, misma pattern |

---

## 3. Cuándo migrar — disparadores concretos

Solo uno de los siguientes justifica la migración:

### 3.1. Trigger: volumen

- **Criterio**: > 100 msg/s sostenido durante una ventana de 1 hora, o > 50k msg/día de forma consistente.
- **Métrica para medirlo**: `aws logs filter-log-events` sobre el listener buscando `batch_flushed` + contar events/s.
- **Síntoma observable**: el lag calculado como `(now - max(__source_ts_ms)) / 1000` supera 60s en horas pico.

### 3.2. Trigger: latencia SLA

- **Criterio**: el producto requiere latencia Mongo→bronze < 10s (ejemplo: dashboards de fraude en tiempo real, monitoring de aprobación de gastos con alertas < 30s).
- **Síntoma observable**: usuarios reportan que "el dashboard tarda en mostrar el expense nuevo".

### 3.3. Trigger: multi-consumer

- **Criterio**: aparece un segundo consumer que necesita los mismos eventos (ej. pipeline de ML para score de fraude, export a Redshift, alerting a Slack).
- **Síntoma observable**: propuestas de arquitectura que replican el listener o duplican la lectura de Mongo.

### 3.4. Trigger: replay

- **Criterio**: incidente donde hay que reprocesar N días de eventos sin re-full-bootstrap desde Mongo.
- **Síntoma observable**: "el silver tiene un bug desde el martes; necesito reprocesar desde ahí sin tocar Mongo".

### 3.5. Trigger: compliance

- **Criterio**: requerimiento regulatorio de audit trail con retention / inmutabilidad verificable por tercero.
- **Síntoma observable**: auditoría externa pide "muéstrame qué eventos de Mongo ocurrieron entre X e Y".

### 3.6. Trigger: SLA 99.9%+

- **Criterio**: el sistema debe tolerar caída de una AZ sin pérdida de eventos.
- **Síntoma observable**: incidentes donde el ECS task se muere y hay pérdida detectable (aunque silver compensa, el hueco en bronze es real).

Si ninguno de estos dispara, Phase B es la elección correcta. La trampa clásica es migrar por prestigio técnico ("queremos Kafka") sin trigger real — duplica costos sin ROI.

---

## 4. Plan de migración (4-6 semanas estimadas)

### Semana 1: Infra base MSK + Debezium

**Terraform nuevo** (~300 LoC):

```hcl
# infra/terraform/msk.tf
resource "aws_msk_serverless_cluster" "nexus" {
  cluster_name = "${var.prefix}-kafka"
  vpc_config { ... }
  client_authentication { sasl { iam { enabled = true } } }
}

# infra/terraform/debezium.tf
resource "aws_ecr_repository" "debezium" { ... }
resource "aws_ecs_task_definition" "debezium" {
  cpu = "1024"; memory = "2048"  # Java agent es más pesado
  container_definitions = jsonencode([{
    image = "${repo}:debezium-2.7"
    environment = [...]
  }])
}

# infra/terraform/schema_registry.tf
resource "aws_glue_registry" "nexus" {
  registry_name = "${var.prefix}-schema-registry"
}
```

**Debezium config** (`debezium-config/application.properties`):

```properties
debezium.source.connector.class=io.debezium.connector.mongodb.MongoDbConnector
debezium.source.mongodb.connection.string=${MONGODB_URI}
debezium.source.topic.prefix=nexus
debezium.source.database.include.list=nexus_dev
debezium.source.collection.include.list=nexus_dev.expenses,nexus_dev.receipts,nexus_dev.hitl_tasks,nexus_dev.ocr_extractions,nexus_dev.expense_events
debezium.source.capture.mode=change_streams_update_full_with_pre_image
debezium.source.snapshot.mode=initial

debezium.transforms=unwrap
debezium.transforms.unwrap.type=io.debezium.connector.mongodb.transforms.ExtractNewDocumentState
debezium.transforms.unwrap.add.fields=op,source.ts_ms
debezium.transforms.unwrap.drop.tombstones=false

debezium.sink.type=kafka
debezium.sink.kafka.producer.bootstrap.servers=${MSK_BOOTSTRAP}
debezium.sink.kafka.producer.security.protocol=SASL_SSL
debezium.sink.kafka.producer.sasl.mechanism=AWS_MSK_IAM
```

**Criterio semana 1**: Debezium Server conectado a Mongo, emite eventos a MSK. Verificar con `kcat -C -b <bootstrap>` sobre `nexus.nexus_dev.expenses`.

### Semana 2: Mongo pre-image + Kafka topic prep

**Mongo**: activar pre/post images para change streams:

```javascript
for (const coll of ["expenses", "receipts", "hitl_tasks", "ocr_extractions", "expense_events"]) {
  db.runCommand({
    collMod: coll,
    changeStreamPreAndPostImages: { enabled: true }
  });
}
```

**Topics**: crear manualmente con retention deseado:

```bash
for t in expenses receipts hitl_tasks ocr_extractions expense_events; do
  kafka-topics --bootstrap-server $MSK --create \
    --topic nexus.nexus_dev.$t \
    --partitions 6 --replication-factor 3 \
    --config retention.ms=604800000 \
    --config cleanup.policy=delete \
    --config compression.type=zstd
done
```

**Validación contra Phase B**: durante 24h, correr ambos pipelines en paralelo (double-write). Comparar:

```sql
-- Desde bronze (Phase B, Autoloader)
SELECT COUNT(*), SUM(HASH(expense_id, __source_ts_ms)) FROM bronze.mongodb_cdc_expenses;

-- Desde un pipeline dummy alimentado por MSK
SELECT COUNT(*), SUM(HASH(expense_id, __source_ts_ms)) FROM bronze_kafka_staging.mongodb_cdc_expenses;
```

Los hashes tienen que coincidir byte-a-byte.

### Semana 3: Nuevo notebook bronze con Kafka source

Reemplaza los 5 notebooks Autoloader por 1 solo con Kafka source:

```python
# nexus-medallion/src/bronze/cdc_kafka_expenses.py
@dlt.table(name="mongodb_cdc_expenses", ...)
def mongodb_cdc_expenses():
    return (
        spark.readStream.format("kafka")
            .option("kafka.bootstrap.servers", spark.conf.get("nexus.msk_bootstrap"))
            .option("subscribe", "nexus.nexus_dev.expenses")
            .option("startingOffsets", "earliest")
            .option("kafka.security.protocol", "SASL_SSL")
            .option("kafka.sasl.mechanism", "AWS_MSK_IAM")
            .load()
            .select(
                from_avro(col("value"), expenses_avro_schema).alias("payload"),
                col("timestamp").alias("_kafka_ts"),
                col("offset").alias("_kafka_offset"),
                col("partition").alias("_kafka_partition"),
            )
            .select("payload.*", "_kafka_ts", "_kafka_offset", "_kafka_partition")
    )
```

### Semana 4: Cutover

1. **T-0**: MSK + Debezium corriendo estable ≥ 7 días.
2. **T+1**: `databricks pipelines stop` sobre `bronze_cdc_pipeline` (Autoloader).
3. **T+2**: `databricks pipelines start` sobre `bronze_kafka_pipeline` con `startingOffsets=earliest`.
4. **T+3**: Primera corrida: verificar que bronze row counts coinciden con Phase B pre-cutover.
5. **T+4**: Silver refresh normal; verificar que no hay gaps.
6. **T+5**: `terraform apply -var=cdc_desired_count=0` para apagar el listener Python.

### Semana 5: Multi-consumer (validación del ROI)

Añadir un segundo consumer para demostrar el beneficio:

- **Opción A**: S3 Sink Connector → parquet hourly partitioning → Athena queries sin pasar por Databricks.
- **Opción B**: ML feature store (Feast + Kafka source) para fraud detection.
- **Opción C**: Consumer Node.js → Slack webhook para alerts de expenses > $5000.

### Semana 6: Tooling y observabilidad

- **Métricas**: CloudWatch metric filters sobre logs Debezium: `MilliSecondsBehindSource`, `MongoDbRecordsPerSecond`.
- **Alertas**:
  - MSK consumer lag > 10k por 5 min → PagerDuty
  - Debezium connector en FAILED → Slack
  - DLQ (nexus.dlq) con > 100 msgs/hora → investigación
- **Dashboard**: Grafana con topics, consumer groups, lag por partition.

---

## 5. Costos comparados

### 5.1. Phase B (hoy) mensual

| Recurso | USD |
|---|---|
| ECS Fargate 256/512 24×7 | 7.50 |
| S3 CDC + PUTs | 0.30 |
| DynamoDB PPR | 0.05 |
| CloudWatch Logs 14d | 0.50 |
| ECR | 0.03 |
| NAT egress Mongo | 3.50 |
| **Total Phase B** | **~11.88** |

### 5.2. Phase B+ mensual (estimado)

| Recurso | USD |
|---|---|
| MSK Serverless baseline | 150.00 |
| MSK ingress/egress (bajo volumen) | 5-15 |
| ECS Fargate 1024/2048 Debezium Server | 30.00 |
| S3 DR backup (opcional) | 0.50 |
| Glue Schema Registry | 0.00 (free tier) |
| CloudWatch métricas custom | 3.00 |
| NAT egress MSK + Mongo | 5-10 |
| **Total Phase B+** | **~195-210** |

**Delta**: +$185/mo. Se justifica solo cuando al menos un trigger del §3 es real.

### 5.3. Alternativas intermedias (si B queda corto pero B+ es caro)

- **Strimzi (Kafka en EKS gestionado por vos)**: ~$80/mo pero requiere operar K8s.
- **Confluent Cloud basic cluster**: ~$50/mo + $0.10/GB → puede ser más barato que MSK si volumen bajo.
- **Redpanda en ECS** (single-broker, no HA): ~$20/mo pero sin replication → datos en riesgo en AZ outage.
- **AWS Kinesis Data Streams**: ~$25/mo baseline + $0.014/MB ingress. Alternativa native sin Kafka, pero no hay conector Debezium directo — habría que escribir un bridge Python → Kinesis.

---

## 6. Riesgos conocidos de la migración

### 6.1. Debezium snapshot inicial duplica eventos

Cuando arrancás Debezium por primera vez con `snapshot.mode=initial`, emite **todos** los documentos actuales con `__op=r`. Si en paralelo nuestro Autoloader ya tiene el mismo snapshot en bronze, silver verá duplicados.

**Mitigación**: silver `apply_changes sequence_by=__source_ts_ms` es idempotente por diseño. El peor caso es 2× rows en bronze y 0× rows adicionales en silver. Verificable con el hash compare de la Semana 2.

### 6.2. Pre-image requiere collMod en producción

El comando `collMod changeStreamPreAndPostImages` no es instantáneo en colecciones grandes. Puede bloquear escrituras ~10-30s.

**Mitigación**: ejecutar en ventana de maintenance. Para nuestras colecciones actuales (miles de docs, no millones), es trivial.

### 6.3. MSK IAM auth en Databricks requiere setup específico

Databricks Kafka source con IAM auth necesita `aws-msk-iam-auth-X.X.X.jar` en el classpath del cluster DLT. Serverless DLT lo soporta nativamente desde DBR 13.3+.

**Mitigación**: validar que nuestro catalog `nexus_dev` pipeline puede leer un topic de prueba ANTES del cutover. Es la parte más propensa a fallar.

### 6.4. Cost creep si olvido apagar el listener Python

Correr ambos pipelines por meses = $25/mo extra innecesarios.

**Mitigación**: en Semana 4 cutover, mismo PR que activa Kafka pipeline también pone `cdc_desired_count=0`. No se mergea uno sin el otro.

---

## 7. Reversión / Rollback plan

Si Phase B+ no funciona como esperado en las primeras 48h post-cutover:

```bash
# 1. Reactivar Phase B
terraform apply -var=cdc_desired_count=1
# El listener arranca con el último resume_token DDB (que NO se borró durante la migración)
# Bootstrap no corre (bootstrap_completed_at existe)
# Watch retoma desde el último token

# 2. Reactivar Autoloader pipeline
databricks pipelines start bronze_cdc_pipeline

# 3. Parar Kafka pipeline
databricks pipelines stop bronze_kafka_pipeline
```

El DDB + S3 bucket de Phase B se dejan intactos durante la migración (no se borran hasta Semana 6 post-validación). Esta asimetría es intencional — Kafka offsets se pierden si apagás MSK, pero DDB/S3 son persistentes baratos.

---

## 8. Decisión explícita a tomar antes de migrar

Para ejecutar Phase B+ hace falta **explícitamente** validar estos puntos:

- [ ] Al menos 1 trigger del §3 está activo y medible
- [ ] Budget aprobado para +$185/mo en cloud
- [ ] Al menos 1 semana de overlap Phase B + Phase B+ en producción para validar hashes
- [ ] Runbook de rollback practicado en staging
- [ ] Consumer secundario identificado (si el trigger fue "multi-consumer")
- [ ] Schema evolution process documentado (Avro compat rules)

Sin los 6 tachados, NO se migra. Phase B es "good enough" hasta demostrar lo contrario.

---

## 9. TL;DR para release notes futuras

> **Phase B** (abr 2026): dev-tier CDC. Python listener en ECS → S3 JSONL → Autoloader → bronze. ~$12/mo. Latencia 35s. Single consumer.
>
> **Phase B+** (fecha futura): prod-tier CDC. Debezium Server → MSK Kafka → Databricks Kafka source → bronze. ~$200/mo. Latencia 3s. Multi-consumer, schema registry, replay 7d. **Mismo envelope, mismas bronze tables, silver/gold intactos**.

---

## Referencias para estudiar

- Debezium MongoDB: https://debezium.io/documentation/reference/stable/connectors/mongodb.html
- Debezium Server: https://debezium.io/documentation/reference/stable/operations/debezium-server.html
- MSK Serverless: https://docs.aws.amazon.com/msk/latest/developerguide/serverless.html
- Databricks + MSK IAM: https://docs.databricks.com/aws/en/connect/streaming/msk
- Schema Registry patterns: https://docs.confluent.io/platform/current/schema-registry/fundamentals/schema-evolution.html
