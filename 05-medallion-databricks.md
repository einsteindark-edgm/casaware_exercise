# 05 · Medallion Architecture — Databricks Bronze/Silver/Gold + Unity Catalog + Vector Search

> **Rol en Nexus.** Este sistema transforma los eventos CDC y salidas de Textract en datos limpios, gobernados y listos para consumo analítico y RAG. Define las tablas Delta, las transformaciones entre capas, las políticas de Unity Catalog, los índices de Vector Search y el Lakehouse Monitoring que vigila el drift del OCR.
>
> **Pre-requisito de lectura:** [`00-contratos-compartidos.md`](./00-contratos-compartidos.md) (catálogo y esquemas) y [`04-cdc-ingestion.md`](./04-cdc-ingestion.md) (input de Bronze).

---

## 1. Principios de la arquitectura

1. **Bronze** = datos crudos append-only. Refleja fielmente lo que llega del upstream (CDC de Mongo, outputs de Textract). No se modifica.
2. **Silver** = datos limpios, deduplicados y tipados. Materialización actual de entidades (expenses viva vs CDC histórica). Aplica calidad y normalización.
3. **Gold** = datos listos para consumo: reportes, RAG, dashboards. Agregados por tenant, chunks listos para embeddings.
4. **Vector** = índices de búsqueda semántica sobre Gold.
5. **Unity Catalog** gobierna permisos, RLS, linaje y data contracts en todas las capas.

---

## 2. Stack técnico

| Componente | Versión / Producto | Notas |
|---|---|---|
| **Databricks Runtime** | 15.4 LTS+ | Spark 3.5+ |
| **Delta Lake** | 3.2+ | CDF, DVs, Liquid Clustering |
| **Unity Catalog** | Activo en la cuenta | Obligatorio |
| **Lakeflow (ex-DLT)** | Lakeflow Spark Declarative Pipelines | Pipelines declarativos |
| **Databricks Jobs** | (ex-Workflows) | Scheduling |
| **Mosaic AI Vector Search** | Storage-Optimized o Standard | Delta Sync Index |
| **Lakehouse Monitoring** | Nativo en UC | Drift de OCR |
| **Orquestación externa** | Databricks Asset Bundles (DAB) | CI/CD |
| **Lenguaje** | Python + SQL | PySpark |

---

## 3. Estructura de directorios del proyecto

```
nexus-medallion/
├── databricks.yml                       # Asset bundle root
├── resources/
│   ├── pipelines/
│   │   ├── bronze.yml                   # (ver doc 04 - aquí solo referencia)
│   │   ├── silver.yml
│   │   └── gold.yml
│   ├── jobs/
│   │   ├── bronze_textract_ingest.yml
│   │   ├── vector_index_refresh.yml
│   │   └── lakehouse_monitoring.yml
│   └── warehouses/
│       └── serverless.yml
│
├── src/
│   ├── silver/
│   │   ├── expenses.py                  # APPLY CHANGES INTO desde bronze
│   │   ├── receipts.py
│   │   ├── hitl_events.py
│   │   └── ocr_extractions.py           # Normaliza textract_raw_output
│   │
│   ├── gold/
│   │   ├── expense_audit.py             # Join silver.expenses + silver.ocr_extractions
│   │   ├── expense_chunks.py            # Genera chunks texto para RAG
│   │   └── kpi_tenant_daily.py
│   │
│   ├── vector/
│   │   ├── create_endpoint.py
│   │   └── create_index.py
│   │
│   ├── governance/
│   │   ├── row_level_security.sql
│   │   ├── column_masks.sql
│   │   └── grants.sql
│   │
│   ├── quality/
│   │   ├── expectations.py              # DLT expectations
│   │   └── great_expectations_suites/   # Checks adicionales
│   │
│   └── monitoring/
│       ├── create_ocr_monitor.py
│       └── alerts_config.py
│
├── tests/
│   ├── unit/
│   ├── integration/                     # Usa DBX connect
│   └── data_contracts/
│
└── docs/
    ├── schemas.md
    ├── lineage.md
    └── runbooks/
```

---

## 4. Capa Bronze — tablas

(Referenciar doc 04 para el detalle de ingesta CDC. Esta sección cataloga las tablas.)

| Tabla | Fuente | Formato | Partición |
|---|---|---|---|
| `nexus_prod.bronze.mongodb_cdc_expenses` | Kafka `nexus.nexus_prod.expenses` | Delta | `_cdc_date` |
| `nexus_prod.bronze.mongodb_cdc_receipts` | Kafka `nexus.nexus_prod.receipts` | Delta | `_cdc_date` |
| `nexus_prod.bronze.mongodb_cdc_hitl_tasks` | Kafka `nexus.nexus_prod.hitl_tasks` | Delta | `_cdc_date` |
| `nexus_prod.bronze.mongodb_cdc_ocr_extractions` | Kafka `nexus.nexus_prod.ocr_extractions` | Delta | `_cdc_date` |
| `nexus_prod.bronze.mongodb_cdc_expense_events` | Kafka `nexus.nexus_prod.expense_events` | Delta | `_cdc_date` |
| `nexus_prod.bronze.textract_raw_output` | S3 `nexus-textract-output-prod/` (Auto Loader) | Delta | `ingestion_date` |
| `nexus_prod.bronze._dlq` | Kafka DLQ + errores pipelines | Delta | `ingestion_date` |

**Cambio clave respecto a versiones previas del diseño.** La tabla `bronze.textract_raw_output` se conserva pero pierde su rol crítico: ya no es la fuente de `silver.ocr_extractions`. Los datos OCR fluyen ahora vía Mongo (`ocr_extractions` colección → CDC → `bronze.mongodb_cdc_ocr_extractions` → `silver.ocr_extractions`). El JSON crudo de Textract en S3 + `bronze.textract_raw_output` quedan solo como **linaje y auditoría**: si en una investigación alguien necesita ver el output exacto de Textract de un día específico, ahí está. Pero no participa del flujo principal.

### 4.1. Ingesta de Textract output — Auto Loader job

```python
# src/bronze/textract_autoloader.py (corre en Databricks Job, no Lakeflow)
from pyspark.sql.functions import input_file_name, current_timestamp, col

bronze_textract = (
    spark.readStream
        .format("cloudFiles")
        .option("cloudFiles.format", "json")
        .option("cloudFiles.schemaLocation", "/Volumes/nexus_prod/bronze/_schemas/textract/")
        .option("cloudFiles.inferColumnTypes", "true")
        .option("cloudFiles.schemaEvolutionMode", "addNewColumns")
        .load("s3://nexus-textract-output-prod/")
        .withColumn("_source_file", input_file_name())
        .withColumn("_ingestion_ts", current_timestamp())
        .withColumn("_ingestion_date", current_timestamp().cast("date"))
        # tenant_id y expense_id se extraen del path: tenant={t}/expense={e}/...json
        .withColumn("tenant_id", regexp_extract("_source_file", r"tenant=([^/]+)", 1))
        .withColumn("expense_id", regexp_extract("_source_file", r"expense=([^/]+)", 1))
)

(bronze_textract.writeStream
    .format("delta")
    .option("checkpointLocation", "/Volumes/nexus_prod/bronze/_checkpoints/textract/")
    .option("mergeSchema", "true")
    .trigger(processingTime="30 seconds")
    .toTable("nexus_prod.bronze.textract_raw_output")
)
```

---

## 5. Capa Silver — tablas vivas

### 5.1. `silver.expenses` — replica viva de MongoDB con APPLY CHANGES INTO

Usar `APPLY CHANGES INTO` en Lakeflow para materializar la última versión de cada expense:

```python
# src/silver/expenses.py
import dlt
from pyspark.sql.functions import col, to_timestamp

@dlt.view
def v_expenses_cdc_cleansed():
    return (
        spark.readStream.table("nexus_prod.bronze.mongodb_cdc_expenses")
            .select(
                col("expense_id"),
                col("tenant_id"),
                col("user_id"),
                col("receipt_id"),
                col("workflow_id"),
                col("amount").cast("decimal(18,2)"),
                col("currency"),
                col("date").cast("date"),
                col("vendor"),
                col("category"),
                col("status"),
                col("created_at").cast("timestamp"),
                col("updated_at").cast("timestamp"),
                col("__op").alias("_op"),
                col("__source_ts_ms").alias("_source_ts_ms"),
                col("__deleted").cast("boolean").alias("_deleted"),
            )
            .where("expense_id IS NOT NULL AND tenant_id IS NOT NULL")
    )

dlt.create_streaming_table(
    name="expenses",
    comment="Vista viva de expenses de MongoDB. Una fila por expense_id.",
    table_properties={
        "quality": "silver",
        "delta.enableChangeDataFeed": "true",
    },
    expect_all={
        "valid_amount": "amount > 0 AND amount < 1000000000",
        "valid_tenant": "tenant_id IS NOT NULL",
        "valid_currency": "currency RLIKE '^[A-Z]{3}$'",
    },
    expect_or_drop={
        "valid_status": "status IN ('pending','processing','hitl_required','approved','rejected','cancelled')",
    },
)

dlt.apply_changes(
    target="expenses",
    source="v_expenses_cdc_cleansed",
    keys=["expense_id"],
    sequence_by=col("_source_ts_ms"),
    apply_as_deletes=expr("_op = 'd'"),
    except_column_list=["_op", "_source_ts_ms", "_deleted"],
    stored_as_scd_type="1",
)
```

**Por qué SCD1 y no SCD2.** MongoDB ya es el sistema de verdad transaccional; el histórico de cambios queda en Bronze. Silver da solo la foto actual para minimizar costos y complejidad de consumo. Si el negocio luego pide auditoría de cambios, activar SCD2 en una tabla `silver.expenses_history` paralela.

### 5.2. `silver.ocr_extractions` — desde CDC de Mongo

**Fuente:** `bronze.mongodb_cdc_ocr_extractions` (CDC de la colección Mongo `ocr_extractions` que el Worker escribe tras invocar Textract). Los datos ya vienen normalizados desde el Worker — el medallion solo replica con SCD1.

```python
# src/silver/ocr_extractions.py
import dlt
from pyspark.sql.functions import col, expr

@dlt.view
def v_ocr_extractions_cdc_cleansed():
    return (
        spark.readStream.table("nexus_prod.bronze.mongodb_cdc_ocr_extractions")
            .select(
                col("expense_id"),
                col("tenant_id"),
                col("user_id"),
                col("ocr_total").cast("decimal(18,2)"),
                col("ocr_total_confidence").cast("double"),
                col("ocr_vendor"),
                col("ocr_vendor_confidence").cast("double"),
                col("ocr_date").cast("date"),
                col("ocr_date_confidence").cast("double"),
                col("ocr_currency"),
                col("avg_confidence").cast("double"),
                col("textract_raw_s3_key"),
                col("extracted_at").cast("timestamp"),
                col("__op").alias("_op"),
                col("__source_ts_ms").alias("_source_ts_ms"),
            )
            .where("expense_id IS NOT NULL")
    )

dlt.create_streaming_table(
    name="ocr_extractions",
    comment="Última extracción OCR por expense, replicada de Mongo vía CDC.",
    table_properties={"quality": "silver", "delta.enableChangeDataFeed": "true"},
    expect_all={
        "valid_confidence": "avg_confidence BETWEEN 0 AND 100",
        "has_total_or_vendor": "ocr_total IS NOT NULL OR ocr_vendor IS NOT NULL",
    },
)

dlt.apply_changes(
    target="ocr_extractions",
    source="v_ocr_extractions_cdc_cleansed",
    keys=["expense_id"],
    sequence_by=col("_source_ts_ms"),
    apply_as_deletes=expr("_op = 'd'"),
    except_column_list=["_op", "_source_ts_ms"],
    stored_as_scd_type="1",
)
```

**Por qué es trivial respecto a la versión previa.** En el diseño anterior, esta tabla se construía haciendo pivot del JSON crudo de Textract, con explosiones y conversiones de tipos manejadas en Spark. Ahora todo eso lo hace el Worker (Python puro, fácil de testear, fácil de iterar) y el medallion solo replica el resultado ya estructurado. Menos código en Spark = menos bugs en producción y deploys más rápidos.

### 5.3. `silver.expense_events` — timeline replicado

```python
@dlt.view
def v_expense_events_cdc_cleansed():
    return (
        spark.readStream.table("nexus_prod.bronze.mongodb_cdc_expense_events")
            .select(
                col("event_id"),
                col("expense_id"),
                col("tenant_id"),
                col("event_type"),
                col("actor"),
                col("details"),
                col("workflow_id"),
                col("created_at").cast("timestamp"),
                col("__op").alias("_op"),
                col("__source_ts_ms").alias("_source_ts_ms"),
            )
            .where("event_id IS NOT NULL")
    )

dlt.create_streaming_table(
    name="expense_events",
    comment="Timeline append-only de eventos por expense. Idéntico a la colección Mongo.",
    table_properties={"quality": "silver"},
)

dlt.apply_changes(
    target="expense_events",
    source="v_expense_events_cdc_cleansed",
    keys=["event_id"],         # event_id es único (ULID), no expense_id
    sequence_by=col("_source_ts_ms"),
    stored_as_scd_type="1",
)
```

**Uso analítico.** Esta tabla habilita queries como "tiempo promedio entre `created` y `approved` por tenant", "% de expenses que pasan por HITL por mes", "qué `actor` resuelve más HITLs". El frontend lee de Mongo para latencia; analytics lee de Silver para escala.

### 5.4. `silver.receipts` y `silver.hitl_events`

Análogas a `silver.expenses` con APPLY CHANGES INTO. `hitl_events` guarda TODO el histórico (SCD2-like) porque es relevante auditar intervenciones humanas.

---

## 6. Capa Gold — datos listos para consumo

### 6.1. `gold.expense_audit` — promoción reactiva al `status='approved'`

**Disparador.** Esta tabla NO se construye con un join general "todos los expenses con sus OCR y HITL". Se construye **reactivamente**: cuando un expense en `silver.expenses` alcanza `status='approved'`, Lakeflow lo materializa a Gold. Cualquier otro estado (`pending`, `processing`, `hitl_required`, `failed`) no genera fila en Gold.

**Por qué este modelo y no un join.** El Worker ya consolidó los campos finales (`final_amount`, `final_vendor`, `final_date`, `source_per_field`) y los embebió en el documento Mongo del expense durante el `update_expense_to_approved`. Lakeflow solo tiene que copiar esos campos a Gold y enriquecer con metadata histórica de los `expense_events`. **No hay decisión de negocio en Gold** — esa decisión la tomó el Worker.

```python
# src/gold/expense_audit.py
import dlt
from pyspark.sql.functions import col, collect_list, struct, max as spark_max

@dlt.view
def v_approved_expenses():
    """Solo expenses recién aprobados que aún no están en Gold."""
    return (
        dlt.read_stream("silver.expenses")
        .where("status = 'approved'")
        .select(
            "tenant_id", "expense_id", "user_id", "receipt_id",
            "final_amount", "final_vendor", "final_date", "final_currency",
            "source_per_field",
            "approved_at",
        )
    )

@dlt.view
def v_expense_event_summary():
    """Para cada expense, agrega su timeline de eventos."""
    return (
        dlt.read("silver.expense_events")
        .groupBy("tenant_id", "expense_id")
        .agg(
            collect_list(struct("event_type", "actor", "created_at", "details"))
                .alias("event_history"),
            spark_max("created_at").alias("last_event_at"),
        )
    )

dlt.create_streaming_table(
    name="expense_audit",
    comment="Un registro final por expense aprobado. Listo para BI, RAG y reportes.",
    table_properties={"quality": "gold", "delta.enableChangeDataFeed": "true"},
    cluster_by=["tenant_id", "final_date"],
)

dlt.apply_changes(
    target="expense_audit",
    source="v_approved_expenses",
    keys=["expense_id"],
    sequence_by=col("approved_at"),
    stored_as_scd_type="1",
)

# Vista enriquecida con history (no reemplaza, complementa)
@dlt.table(
    name="expense_audit_with_history",
    comment="expense_audit + el timeline completo de eventos. Usado por dashboards forenses.",
)
def expense_audit_with_history():
    return (
        dlt.read("expense_audit").alias("a")
        .join(dlt.read("v_expense_event_summary").alias("h"),
              on=["tenant_id", "expense_id"], how="left")
    )
```

**Idempotencia.** Si un expense ya en Gold se "re-aprueba" (caso patológico: HITL reabierto), `apply_changes` con `sequence_by=approved_at` se queda con la versión más reciente. Si Gold se trunca y se reconstruye, Lakeflow re-emite todas las filas desde el cursor inicial de Silver — cero pérdida.

**Latencia esperada.** Mongo update → CDC → Bronze (~10s) → Silver APPLY CHANGES (~1-2 min) → Silver detecta status='approved' → Gold (~30s). Total ~2-3 minutos desde que el Worker termina hasta que el expense aparece en Gold y empieza a indexarse en Vector Search.

### 6.2. `gold.expense_chunks` — chunks para RAG

Una fila por "chunk" de texto indexable. Se genera concatenando información estructurada:

```python
@dlt.table(name="expense_chunks")
def gold_expense_chunks():
    return (
        dlt.read("gold.expense_audit")
        .select(
            col("tenant_id"),
            col("expense_id"),
            concat_ws(" | ",
                lit("Gasto de"),  col("final_vendor"),
                lit("por"),       col("final_amount").cast("string"),
                col("user_reported_currency"),
                lit("el"),        col("final_date").cast("string"),
                lit("categoría:"), col("category"),
            ).alias("chunk_text"),
            col("final_amount").alias("amount"),
            col("final_vendor").alias("vendor"),
            col("final_date").alias("date"),
            col("category"),
            col("audited_at"),
        )
        .withColumn("chunk_id", concat_ws("_", "expense_id", lit("main")))
    )
```

**Nota sobre chunking**: por ahora un chunk por expense es suficiente (los gastos son pequeños). Cuando se añada soporte a facturas con muchos line items, generar chunks adicionales por línea.

### 6.3. `gold.kpi_tenant_daily`

Agregado para dashboards, refresh nocturno:

```sql
CREATE OR REPLACE TABLE nexus_prod.gold.kpi_tenant_daily AS
SELECT
    tenant_id,
    date,
    category,
    COUNT(*) AS expense_count,
    SUM(final_amount) AS total_amount,
    AVG(ocr_avg_confidence) AS avg_ocr_confidence,
    SUM(CASE WHEN hitl_decision IS NOT NULL THEN 1 ELSE 0 END) AS hitl_count
FROM nexus_prod.gold.expense_audit
WHERE status = 'approved'
GROUP BY tenant_id, date, category;
```

---

## 7. Vector Search — índice sobre chunks

### 7.1. Creación del endpoint

```python
# src/vector/create_endpoint.py
from databricks.vector_search.client import VectorSearchClient

client = VectorSearchClient()
client.create_endpoint(
    name="nexus-vs-endpoint",
    endpoint_type="STANDARD",   # usar STORAGE_OPTIMIZED si >10M chunks
)
```

### 7.2. Pre-requisito: Change Data Feed en la tabla source

```sql
ALTER TABLE nexus_prod.gold.expense_chunks
  SET TBLPROPERTIES (delta.enableChangeDataFeed = true);
```

### 7.3. Creación del índice Delta Sync

```python
# src/vector/create_index.py
client.create_delta_sync_index(
    endpoint_name="nexus-vs-endpoint",
    source_table_name="nexus_prod.gold.expense_chunks",
    index_name="nexus_prod.vector.expense_chunks_index",
    primary_key="chunk_id",
    pipeline_type="CONTINUOUS",      # Sincroniza en ~5 min al cambiar source
    embedding_source_column="chunk_text",
    embedding_model_endpoint_name="databricks-bge-large-en",
    # CRÍTICO: columnas a incluir como metadata filtrable
    columns_to_sync=[
        "chunk_id", "tenant_id", "expense_id",
        "chunk_text", "amount", "vendor", "date", "category",
    ],
)
```

Opciones del modelo de embeddings:
- **`databricks-bge-large-en`** (Foundation Model API, pay-per-token) — recomendado para arranque, 1024 dim.
- **`databricks-gte-large-en`** — alternativa comparable.
- Self-hosted: desplegar `nomic-embed-text` o `mxbai-embed-large` en Model Serving si se requiere data residency fuerte.

### 7.4. Query (referencia)

El worker de Temporal (doc 03) consulta así:

```python
results = index.similarity_search(
    query_text="¿cuánto gasté en café en febrero?",
    columns=["chunk_id", "expense_id", "chunk_text", "amount", "vendor", "date"],
    num_results=5,
    filters='tenant_id = "t_01HQ..."',   # CRÍTICO: inyectado por el backend, no por el LLM
    query_type="HYBRID",
)
```

**⚠️ Seguridad del filtro.** Vector Search NO soporta RLS de Unity Catalog. El aislamiento multi-tenant depende **enteramente** de que el filtro `tenant_id=` se aplique en cada query. El doc 03 documenta que el tool Exoclaw inyecta esto a nivel del activity, no lo escribe el LLM.

---

## 8. Unity Catalog — gobernanza

### 8.1. Jerarquía y grants

```sql
-- Crear catálogo por entorno
CREATE CATALOG IF NOT EXISTS nexus_prod MANAGED LOCATION 's3://nexus-uc-prod/';

-- Schemas
CREATE SCHEMA nexus_prod.bronze;
CREATE SCHEMA nexus_prod.silver;
CREATE SCHEMA nexus_prod.gold;
CREATE SCHEMA nexus_prod.vector;
CREATE SCHEMA nexus_prod.security;  -- funciones de RLS

-- Grants base
GRANT USAGE ON CATALOG nexus_prod TO `nexus-engineering`;
GRANT CREATE SCHEMA ON CATALOG nexus_prod TO `nexus-engineering`;

-- Silver y Gold: lectura para apps de producción
GRANT SELECT ON SCHEMA nexus_prod.silver TO `nexus-app-sp`;   -- service principal del backend
GRANT SELECT ON SCHEMA nexus_prod.gold   TO `nexus-app-sp`;
GRANT SELECT ON TABLE  nexus_prod.vector.expense_chunks_index TO `nexus-app-sp`;

-- Bronze: solo data engineers
GRANT SELECT ON SCHEMA nexus_prod.bronze TO `nexus-data-engineers`;
```

### 8.2. Row-Level Security (RLS)

Databricks Unity Catalog soporta RLS vía **Row Filters**:

```sql
-- Función que decide si la fila es visible
CREATE OR REPLACE FUNCTION nexus_prod.security.tenant_filter(row_tenant_id STRING)
RETURN CASE
  WHEN is_account_group_member('nexus-admins') THEN TRUE
  WHEN current_user() = 'nexus-app-sp' THEN
      row_tenant_id = session_user_attribute('tenant_id')
  ELSE FALSE
END;

-- Aplicar a las tablas
ALTER TABLE nexus_prod.silver.expenses
  SET ROW FILTER nexus_prod.security.tenant_filter ON (tenant_id);

ALTER TABLE nexus_prod.gold.expense_audit
  SET ROW FILTER nexus_prod.security.tenant_filter ON (tenant_id);
```

**Importante:** cuando el backend llama a Databricks SQL con un service principal compartido, no hay un `session_user_attribute("tenant_id")` natural. Dos soluciones:

1. **Service principal por tenant** (complejo operativamente).
2. **SET en sesión** antes de cada query: `SET tenant_id = 't_01HQ...'` y RLS lee `getvariable("tenant_id")`. El backend hace esto al abrir cada conexión.

Opción 2 es la recomendada. Documentar que el `set` debe ser la **primera sentencia** de cada transacción.

### 8.3. Column masks para PII

```sql
CREATE OR REPLACE FUNCTION nexus_prod.security.mask_email(email STRING)
RETURN CASE
  WHEN is_account_group_member('nexus-admins') THEN email
  ELSE regexp_replace(email, '(.{2}).*@', '$1****@')
END;

ALTER TABLE nexus_prod.silver.receipts ALTER COLUMN user_email
  SET MASK nexus_prod.security.mask_email;
```

### 8.4. Lineage

Unity Catalog captura linaje **automáticamente** cuando se usa Lakeflow o Spark SQL. Visible en el Catalog Explorer. Sin configuración adicional.

---

## 9. Data Quality — Expectations

Dentro de Lakeflow, expectativas por tabla:

```python
@dlt.table(
    name="expenses",
    expect_all={
        "valid_amount": "amount > 0 AND amount < 1e9",
        "valid_tenant": "tenant_id IS NOT NULL AND tenant_id RLIKE '^t_'",
    },
    expect_all_or_drop={
        "valid_currency": "currency RLIKE '^[A-Z]{3}$'",
    },
    expect_all_or_fail={
        "pk_present": "expense_id IS NOT NULL",
    },
)
```

Políticas:
- `expect_all`: logged, no bloquea.
- `expect_all_or_drop`: fila descartada si falla.
- `expect_all_or_fail`: pipeline ABORT. Solo para invariantes críticos.

Dashboard de calidad: cada pipeline Lakeflow expone una vista de violaciones por expectation en el UI.

---

## 10. Lakehouse Monitoring — detección de drift del OCR

Monitorear que la calidad del OCR no se degrade silenciosamente.

```python
# src/monitoring/create_ocr_monitor.py
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.catalog import (
    MonitorInferenceLog, MonitorMetric, MonitorMetricType,
    MonitorTimeSeries, MonitorInfoStatus,
)

w = WorkspaceClient()

w.quality_monitors.create(
    table_name="nexus_prod.silver.ocr_extractions",
    output_schema_name="nexus_prod.silver._monitoring",
    assets_dir="/Shared/nexus/monitors/ocr",

    time_series=MonitorTimeSeries(
        timestamp_col="extracted_at",
        granularities=["1 day", "1 hour"],
    ),

    slicing_exprs=["tenant_id"],

    custom_metrics=[
        MonitorMetric(
            name="avg_ocr_confidence",
            type=MonitorMetricType.CUSTOM_METRIC_TYPE_AGGREGATE,
            definition="AVG(avg_confidence)",
            input_columns=[":table"],
            output_data_type="DOUBLE",
        ),
        MonitorMetric(
            name="pct_low_confidence_records",
            type=MonitorMetricType.CUSTOM_METRIC_TYPE_AGGREGATE,
            definition="AVG(CASE WHEN avg_confidence < 80 THEN 1.0 ELSE 0.0 END)",
            input_columns=[":table"],
            output_data_type="DOUBLE",
        ),
    ],
)
```

**Alerta SQL** sobre la tabla `_profile_metrics` generada:

```sql
SELECT tenant_id, window, avg_ocr_confidence, pct_low_confidence_records
FROM nexus_prod.silver._monitoring.ocr_extractions_profile_metrics
WHERE granularity = '1 day'
  AND window.start >= current_date() - INTERVAL 1 DAY
  AND (avg_ocr_confidence < 85 OR pct_low_confidence_records > 0.1);
```

Programar en Databricks SQL Alerts con notificación a Slack/PagerDuty.

---

## 11. Orquestación con Databricks Jobs / Asset Bundles

`databricks.yml`:

```yaml
bundle:
  name: nexus-medallion

variables:
  env:
    default: dev

targets:
  dev: { mode: development, workspace: { host: "..." } }
  prod: { mode: production,  workspace: { host: "..." } }

resources:
  pipelines:
    silver_pipeline:
      name: "nexus-silver-${var.env}"
      catalog: "nexus_${var.env}"
      target: "silver"
      libraries:
        - notebook: { path: ./src/silver/expenses.py }
        - notebook: { path: ./src/silver/receipts.py }
        - notebook: { path: ./src/silver/hitl_events.py }
        - notebook: { path: ./src/silver/ocr_extractions.py }
      continuous: true
      channel: CURRENT

    gold_pipeline:
      name: "nexus-gold-${var.env}"
      catalog: "nexus_${var.env}"
      target: "gold"
      libraries:
        - notebook: { path: ./src/gold/expense_audit.py }
        - notebook: { path: ./src/gold/expense_chunks.py }
      continuous: true

  jobs:
    bronze_textract_ingest:
      name: "nexus-bronze-textract-${var.env}"
      tasks:
        - task_key: autoloader
          notebook_task: { notebook_path: ./src/bronze/textract_autoloader.py }
          job_cluster_key: stream_cluster
      job_clusters:
        - job_cluster_key: stream_cluster
          new_cluster:
            spark_version: "15.4.x-scala2.12"
            node_type_id: "i3.xlarge"
            num_workers: 2

    vector_refresh:
      name: "nexus-vector-refresh-${var.env}"
      schedule: { quartz_cron_expression: "0 0 * * * ?" }   # Hourly fallback sync
      tasks:
        - task_key: sync
          notebook_task: { notebook_path: ./src/vector/trigger_sync.py }
```

Deploy: `databricks bundle deploy --target prod`.

---

## 12. Costos — consideraciones

1. **Clusters serverless** para SQL Warehouse (queries del backend) y para Lakeflow — pagas por uso real.
2. **Liquid Clustering** en tablas Silver/Gold en lugar de partitions tradicionales:
   ```sql
   ALTER TABLE nexus_prod.silver.expenses CLUSTER BY (tenant_id, date);
   ```
   Evita la explosión de small files en multi-tenant.
3. **Vector Search Standard** escala a 0 cuando no hay queries — ideal para dev. Production con QPS bajo (<5) también cabe en Standard.
4. **Retention de Bronze**: aplicar `VACUUM` con retención de 7 días tras probar que Silver es idempotente ante replay.

---

## 13. Criterios de aceptación

1. **Latencia Mongo → Silver**: cambio en MongoDB visible en `silver.expenses` en **≤ 2 minutos p95** (10s CDC + 1min batch streaming).
2. **Latencia Textract → Vector Search**: archivo escrito en S3 → chunk indexado en Vector Search en **≤ 15 minutos** (Auto Loader 30s + Silver 1m + Gold 1m + VS continuous sync 5-10m).
3. **Multi-tenant aislado**: un usuario del tenant A que ejecute `SELECT * FROM silver.expenses` no ve filas del tenant B. Validado con tests E2E con dos tenants.
4. **Filtro Vector Search no falla silenciosamente**: consulta sin `tenant_id` en filters → el worker de Temporal la rechaza antes de enviarla (test en doc 03).
5. **Data Quality**: datos que violan `valid_currency` NO llegan a Silver (descartados + logueados).
6. **Drift**: bajada de `avg_ocr_confidence` por debajo de 85% durante 1h dispara alerta a Slack en ≤ 5 min.
7. **Lineage**: en UC Catalog Explorer, la tabla `gold.expense_audit` muestra upstream a `silver.expenses` + `silver.ocr_extractions` + `silver.hitl_events`.
8. **Recuperación**: truncar `silver.expenses` y re-ejecutar la pipeline reconstruye el estado exacto desde Bronze.

---

## 14. Referencias

- Lakeflow / DLT CDC: https://docs.databricks.com/aws/en/dlt/cdc.html
- Mosaic AI Vector Search: https://docs.databricks.com/aws/en/vector-search/vector-search
- Unity Catalog RLS: https://docs.databricks.com/aws/en/tables/row-and-column-filters.html
- Lakehouse Monitoring: https://docs.databricks.com/aws/en/lakehouse-monitoring/index.html
- Databricks Asset Bundles: https://docs.databricks.com/aws/en/dev-tools/bundles/index.html
- Liquid Clustering: https://docs.databricks.com/aws/en/delta/clustering.html
