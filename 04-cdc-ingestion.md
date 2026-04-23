# 04 · CDC Ingestion — MongoDB → Debezium → Kafka → Bronze

> **Rol en Nexus.** Este sistema captura en near real-time todos los cambios (inserts/updates/deletes) de las colecciones de MongoDB y los entrega a la capa Bronze del Lakehouse Databricks. Es la puerta de entrada de los datos operacionales al mundo analítico. No tiene lógica de negocio — es un pipeline de replicación fiel.
>
> **Pre-requisito de lectura:** [`00-contratos-compartidos.md`](./00-contratos-compartidos.md). Las colecciones MongoDB y el layout de Bronze se definen ahí.

---

## 1. Decisión: Debezium + Kafka (vs AWS DMS)

Elegimos **Debezium MongoDB Source Connector + Kafka + Kafka Connect S3 Sink** como pipeline CDC. Razones:

| Criterio | Debezium + Kafka | AWS DMS |
|---|---|---|
| Integración con MongoDB | Nativa vía Change Streams (oplog), maduro | Soporta MongoDB pero con limitaciones en tipos de cambio |
| Formato de eventos | JSON/Avro ricos con before/after/op/ts_ms | JSON simple, menos expresivo |
| Evolución de esquema | Schema Registry (Avro) o raw JSON | Limitado |
| Replay / Reset | Kafka retiene events, replay a cualquier offset | No retiene — si se pierde, pérdida de datos |
| Backpressure | Kafka absorbe picos | DMS acopla fuerte source/sink |
| Costo operacional | Mayor (hay que operar Kafka) | Menor (managed) |
| Ecosistema conectores | Rico (S3, JDBC, Elastic, etc.) | Limitado a targets AWS |

Como Nexus ya necesita **garantías fuertes** (auditoría financiera, Bronze append-only) y **desacoplamiento** (para añadir otros consumidores en el futuro: alertas fraude, warehouse BI), Debezium + Kafka es la elección correcta aunque implique más operación.

**Alternativa "managed"**: Confluent Cloud (Kafka + Debezium + S3 Sink todos gestionados) si el equipo no quiere operar Kafka. Misma arquitectura lógica.

---

## 2. Arquitectura del pipeline

```
┌────────────────┐
│ MongoDB Atlas  │
│  (Replica Set) │
│                │
│ Change Streams │◀── oplog monitoring
└───────┬────────┘
        │
        ▼
┌──────────────────────────────────────────┐
│ Kafka Connect cluster                    │
│  ┌────────────────────────────────────┐  │
│  │ Debezium MongoDB Source Connector  │  │
│  │  • Emite eventos por colección     │  │
│  │  • Serializa a Avro + Schema Reg.  │  │
│  └─────────────┬──────────────────────┘  │
└────────────────┼─────────────────────────┘
                 ▼
      ┌────────────────────┐
      │  Apache Kafka      │
      │                    │
      │  Topics:           │
      │  • nexus.expenses  │
      │  • nexus.receipts  │
      │  • nexus.hitl_tasks│
      └─────────┬──────────┘
                │
   ┌────────────┼─────────────────┐
   ▼                              ▼
┌──────────────────┐     ┌────────────────────┐
│ S3 Sink          │     │ Databricks         │
│ Connector        │     │ Kafka Source       │
│                  │     │ (Structured        │
│ → S3 parquet     │     │  Streaming)        │
│   hourly rolling │     │ → Bronze tables    │
└──────────────────┘     └────────────────────┘

        ┌──────────────┐
        │ ENFOQUE ELEGIDO:                  │
        │ Databricks Structured Streaming   │
        │ consume Kafka y escribe Bronze    │
        │ directamente con APPLY CHANGES.   │
        │ El S3 sink queda como DR backup.  │
        └──────────────┘
```

---

## 3. Stack técnico

| Componente | Versión | Notas |
|---|---|---|
| **MongoDB** | 7.0+ (Atlas M10+) | Replica set obligatorio para change streams |
| **Kafka** | 3.7+ (Confluent Platform 7.6 o Strimzi en K8s) | Topics con retention alta para replay |
| **Kafka Connect** | 3.7+ | Cluster de al menos 3 nodos en prod |
| **Debezium MongoDB Connector** | **2.7+** | Usa Change Streams API (no oplog directo) |
| **Schema Registry** | Confluent Schema Registry 7.6+ | Para Avro evolution |
| **S3 Sink Connector** | Confluent 10.5+ | Backup paralelo en S3 parquet |
| **Databricks** | Runtime 15.4+ LTS | Structured Streaming consumer |

---

## 4. Configuración de MongoDB

### 4.1. Pre-requisitos

- **Replica set** activo. MongoDB Atlas lo provee por defecto.
- Enable **Change Streams** (default en 4.0+).
- Usuario de Debezium con rol mínimo:
  ```javascript
  db.createRole({
    role: "debeziumRole",
    privileges: [
      { resource: { db: "nexus_prod", collection: "" },
        actions: ["find", "changeStream"] },
      { resource: { db: "local", collection: "oplog.rs" },
        actions: ["find"] }
    ],
    roles: []
  });
  db.createUser({
    user: "debezium",
    pwd: "<secret>",
    roles: [{ role: "debeziumRole", db: "admin" }]
  });
  ```

### 4.2. Colecciones monitoreadas

Según contrato §2.6: `expenses`, `receipts`, `hitl_tasks`. Pattern inclusivo en el connector:
```
collection.include.list=nexus_prod.expenses,nexus_prod.receipts,nexus_prod.hitl_tasks
```

---

## 5. Configuración de Debezium MongoDB Connector

### 5.1. Archivo de configuración (JSON)

Registrar vía `POST http://kafka-connect:8083/connectors`:

```json
{
  "name": "nexus-mongodb-source",
  "config": {
    "connector.class": "io.debezium.connector.mongodb.MongoDbConnector",
    "tasks.max": "1",

    "mongodb.connection.string": "mongodb+srv://debezium:<pass>@nexus-cluster.mongodb.net/?replicaSet=atlas-xxx-shard-0&authSource=admin&ssl=true",

    "topic.prefix": "nexus",
    "database.include.list": "nexus_prod",
    "collection.include.list": "nexus_prod.expenses,nexus_prod.receipts,nexus_prod.hitl_tasks",

    "capture.mode": "change_streams_update_full_with_pre_image",
    "_comment_capture_mode": "Incluye el documento COMPLETO antes y después del update. Requiere 'changeStreamPreAndPostImages' en la colección.",

    "snapshot.mode": "initial",
    "_comment_snapshot_mode": "initial = snapshot inicial + luego change stream. 'never' solo para cuando se resetea con data ya backfilled",

    "key.converter": "io.confluent.connect.avro.AvroConverter",
    "key.converter.schema.registry.url": "http://schema-registry:8081",
    "value.converter": "io.confluent.connect.avro.AvroConverter",
    "value.converter.schema.registry.url": "http://schema-registry:8081",

    "transforms": "unwrap",
    "transforms.unwrap.type": "io.debezium.connector.mongodb.transforms.ExtractNewDocumentState",
    "transforms.unwrap.drop.tombstones": "false",
    "transforms.unwrap.delete.handling.mode": "rewrite",
    "transforms.unwrap.add.fields": "op,source.ts_ms,source.collection",

    "heartbeat.interval.ms": "10000",
    "_comment_heartbeat": "Emite heartbeats aún sin cambios. Evita que el offset quede viejo en colecciones inactivas.",

    "errors.tolerance": "all",
    "errors.log.enable": "true",
    "errors.log.include.messages": "true",
    "errors.deadletterqueue.topic.name": "nexus.dlq",
    "errors.deadletterqueue.topic.replication.factor": "3"
  }
}
```

### 5.2. Activar Change Stream Pre/Post Images en MongoDB

En Atlas o mongo shell:
```javascript
db.runCommand({
  collMod: "expenses",
  changeStreamPreAndPostImages: { enabled: true }
});
// Repetir para receipts y hitl_tasks
```

### 5.3. Topics generados

Debezium creará automáticamente:
- `nexus.nexus_prod.expenses`
- `nexus.nexus_prod.receipts`
- `nexus.nexus_prod.hitl_tasks`

Con la transformation `ExtractNewDocumentState` (unwrap), cada mensaje tiene estructura plana:

```json
{
  "_id": "65a1b2c3d4e5f6789",
  "expense_id": "exp_01HQ...",
  "tenant_id": "t_01HQ...",
  "amount": 100.50,
  "currency": "COP",
  "date": "2026-04-22",
  "vendor": "Starbucks",
  "status": "pending",
  "updated_at": "2026-04-22T15:30:00.000Z",
  "__op": "c",                               // c=create, u=update, d=delete, r=read
  "__source_ts_ms": 1745331000000,
  "__source_collection": "expenses",
  "__deleted": "false"
}
```

### 5.4. Topics config

Crear los topics manualmente para controlar retention:

```bash
kafka-topics --bootstrap-server kafka:9092 --create \
  --topic nexus.nexus_prod.expenses \
  --partitions 6 --replication-factor 3 \
  --config retention.ms=604800000 \
  --config cleanup.policy=delete \
  --config compression.type=zstd
```

- **Partitions = 6**: paralelismo en consumers. Clave de partición es `expense_id` (garantiza orden por expense).
- **Retention 7 días**: suficiente para replay tras incidentes. Backup a largo plazo va al S3 sink.
- **DLQ**: `nexus.dlq` con retention 30 días.

---

## 6. Ingesta a Databricks Bronze (Structured Streaming)

### 6.1. Job declarativo con Lakeflow (antes DLT)

Usar **Lakeflow Spark Declarative Pipelines** (SDP). Un pipeline con una notebook por colección. Ejemplo para `expenses`:

```python
# notebook: bronze/mongodb_cdc_expenses.py
import dlt   # Lakeflow alias
from pyspark.sql.functions import col, from_json, current_timestamp

@dlt.table(
    name="mongodb_cdc_expenses",
    comment="Raw CDC events from MongoDB expenses collection via Debezium",
    table_properties={
        "delta.enableChangeDataFeed": "true",
        "quality": "bronze",
        "pipelines.reset.allowed": "false",   # append-only, no resetear
    },
    partition_cols=["_cdc_date"],
)
def bronze_expenses_cdc():
    return (
        spark.readStream
            .format("kafka")
            .option("kafka.bootstrap.servers", spark.conf.get("kafka.bootstrap.servers"))
            .option("subscribe", "nexus.nexus_prod.expenses")
            .option("startingOffsets", "earliest")
            .option("failOnDataLoss", "false")
            .option("kafka.security.protocol", "SASL_SSL")
            .option("kafka.sasl.mechanism", "PLAIN")
            .option("kafka.sasl.jaas.config", _jaas_config())
            .load()
            # Kafka payload es Avro — decodificar con schema registry
            .select(
                from_avro("value", expenses_avro_schema).alias("payload"),
                col("timestamp").alias("kafka_ts"),
                col("offset"),
                col("partition"),
            )
            .select("payload.*", "kafka_ts", "offset", "partition")
            .withColumn("_cdc_ingestion_ts", current_timestamp())
            .withColumn("_cdc_date", col("_cdc_ingestion_ts").cast("date"))
    )
```

Por qué `readStream` de Kafka directamente en Databricks:
- Evita el intermediario S3 (latencia menor, ~2s p95).
- Structured Streaming maneja checkpointing en DBFS/UC automáticamente.
- Costos menores (no doble storage).

**El S3 Sink Connector sigue activo en paralelo como backup de Disaster Recovery**, no como input crítico.

### 6.2. Esquema Avro (versión simplificada)

El schema lo genera Debezium automáticamente. Para consumir desde Databricks sin acoplarse al Schema Registry en runtime, opción:

1. **Descargar schemas** desde Schema Registry al arrancar pipelines (archivado en volumen Unity Catalog).
2. Usar `from_avro` con el schema cargado.

Alternativa más simple para arranque: **usar JSON en lugar de Avro** (Debezium `JsonConverter`), a costa de no tener validación de schema. Aceptable para v1 si se tienen asserts de calidad en Silver.

### 6.3. Manejo de Deletes

Con `delete.handling.mode=rewrite`, los deletes llegan como un evento con `__deleted=true` y `__op=d`. La tabla Bronze los conserva (append-only).
La eliminación "lógica" se aplica recién en Silver con `APPLY CHANGES INTO ... APPLY AS DELETE WHEN __op = 'd'`.

---

## 7. Backup paralelo: S3 Sink Connector

Configuración de DR (no crítica para el flujo principal):

```json
{
  "name": "nexus-s3-sink",
  "config": {
    "connector.class": "io.confluent.connect.s3.S3SinkConnector",
    "tasks.max": "3",
    "topics.regex": "nexus\\.nexus_prod\\..*",

    "s3.bucket.name": "nexus-cdc-sink-prod",
    "s3.region": "us-east-1",

    "storage.class": "io.confluent.connect.s3.storage.S3Storage",
    "format.class": "io.confluent.connect.s3.format.parquet.ParquetFormat",
    "partitioner.class": "io.confluent.connect.storage.partitioner.TimeBasedPartitioner",
    "partition.duration.ms": "3600000",
    "path.format": "'year'=YYYY/'month'=MM/'day'=dd/'hour'=HH",
    "timestamp.extractor": "Record",

    "flush.size": "10000",
    "rotate.interval.ms": "3600000",

    "schema.compatibility": "BACKWARD"
  }
}
```

Retención en S3: `StorageClass` Standard → IA tras 30 días → Glacier tras 90.

---

## 8. Monitoreo del pipeline

### 8.1. Métricas clave

| Métrica | Fuente | Umbral alerta |
|---|---|---|
| Lag del consumer Databricks (offsets) | Kafka `kafka_consumergroup_group_lag` | > 10000 por 5 min |
| Lag de Debezium | JMX `MilliSecondsBehindSource` | > 30000 ms |
| Errores en connector | Kafka Connect REST `/connectors/nexus-mongodb-source/status` | `FAILED` state |
| Tamaño DLQ | `nexus.dlq` message count | > 100 en 1 hora |
| Tasa de eventos Bronze | Delta `DESCRIBE HISTORY ...` | Drop del 50% vs baseline |

### 8.2. Alerting

- **PagerDuty** integrado con CloudWatch Alarms.
- **Slack** `#nexus-data-alerts` para warnings no críticos.

### 8.3. Dead Letter Queue

Consumidor del topic `nexus.dlq` que escribe a una tabla Delta `nexus_prod.bronze._dlq` para inspección. Incluye campos:
- `topic_original`
- `partition`
- `offset`
- `payload_raw` (base64)
- `error_message`
- `received_at`

---

## 9. Estructura del proyecto (IaC)

```
nexus-cdc/
├── terraform/
│   ├── main.tf                          # VPC, MSK (Kafka), IAM
│   ├── kafka-topics.tf                  # Creación de topics con config
│   ├── iam-debezium.tf
│   └── iam-databricks-access.tf
│
├── kafka-connect/
│   ├── Dockerfile                       # Base Confluent + plugins
│   ├── connectors/
│   │   ├── nexus-mongodb-source.json
│   │   ├── nexus-s3-sink.json
│   │   └── nexus-dlq-to-delta.json      # Sink del DLQ
│   └── scripts/
│       ├── register-connectors.sh
│       ├── validate-config.sh
│       └── reset-offsets.sh             # Runbook para incidentes
│
├── databricks/
│   ├── bronze_pipeline/
│   │   ├── databricks.yml               # Asset bundle
│   │   ├── notebooks/
│   │   │   ├── bronze_expenses_cdc.py
│   │   │   ├── bronze_receipts_cdc.py
│   │   │   └── bronze_hitl_tasks_cdc.py
│   │   └── schemas/
│   │       └── avro/
│   │           ├── expenses.avsc
│   │           ├── receipts.avsc
│   │           └── hitl_tasks.avsc
│   └── jobs/
│       └── bronze_textract_output.py    # Job separado lee s3://nexus-textract-output/ → bronze.textract_raw_output
│
├── mongodb/
│   ├── enable-pre-post-images.js
│   └── create-debezium-user.js
│
└── runbooks/
    ├── reset-connector-snapshot.md
    ├── handle-dlq-events.md
    └── disaster-recovery.md
```

---

## 10. Consideraciones especiales

### 10.1. Datos sensibles (PII)

- Los campos `email`, `tax_id` pueden estar en los eventos CDC. 
- Usar Single Message Transform (SMT) de Debezium para enmascarar en Kafka:
  ```
  transforms.mask.type=org.apache.kafka.connect.transforms.MaskField$Value
  transforms.mask.fields=email
  transforms.mask.replacement=REDACTED
  ```
- Alternativa (preferida): NO aplicar masking en Kafka; dejar que Unity Catalog aplique column masks en Silver/Gold según roles.

### 10.2. Orden global

Partition key = `expense_id` garantiza orden **por expense**. No hay orden global entre expenses (ni se necesita). Documentar que la tabla Bronze puede tener eventos de expenses distintos entrelazados.

### 10.3. Inicialización (snapshot inicial)

La primera vez que el connector arranca con `snapshot.mode=initial`:
- Lee todas las colecciones.
- Genera eventos con `__op=r` (read) para cada documento.
- Luego empieza a seguir change streams.

Para grandes volúmenes (> 10M docs), considerar `snapshot.mode=initial_only` + proceso aparte de streaming. Para volúmenes actuales esperados (< 1M docs) el default funciona.

### 10.4. Upgrade de schema

Cuando el backend añade un campo a `expenses`:
1. Debezium lo detecta automáticamente en el próximo evento.
2. Schema Registry versiona el schema Avro.
3. La tabla Bronze Delta evoluciona con `schemaEvolution=true`.
4. Silver DEBE manejarlo: añadir coalesce a defaults o regenerar con `APPLY CHANGES INTO`.

---

## 11. Criterios de aceptación

1. **Latencia**: un insert en MongoDB aparece en `nexus_prod.bronze.mongodb_cdc_expenses` en **≤ 10 segundos p95**.
2. **Durabilidad**: matar el connector durante un burst de 10K inserts y reanudar → cero eventos perdidos (verificado con conteo).
3. **Deletes**: un `db.expenses.deleteOne(...)` produce un evento con `__deleted=true` y `__op=d` en Bronze.
4. **Schema evolution**: añadir un campo a `expenses` en MongoDB se refleja en Bronze sin downtime.
5. **DLQ**: eventos malformados artificialmente generados terminan en `nexus_prod.bronze._dlq`, no rompen el pipeline.
6. **Observabilidad**: dashboard Grafana con lag, throughput y error rate por topic.
7. **Multi-tenant**: los eventos **incluyen** `tenant_id` (ya viene del documento Mongo). La capa Bronze es agnóstica al tenant; el filtrado lo hace Unity Catalog en Silver.

---

## 12. Referencias

- Debezium MongoDB Connector: https://debezium.io/documentation/reference/stable/connectors/mongodb.html
- Databricks Lakeflow + Kafka: https://docs.databricks.com/aws/en/ingestion/streaming.html
- MongoDB Change Streams Pre/Post Images: https://www.mongodb.com/docs/manual/changeStreams/#change-streams-with-document-pre--and-post-images
- Confluent S3 Sink: https://docs.confluent.io/kafka-connectors/s3-sink/current/overview.html
