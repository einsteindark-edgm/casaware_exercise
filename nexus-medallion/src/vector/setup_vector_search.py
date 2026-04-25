# Databricks notebook source
# MAGIC %md
# MAGIC # Vector Search setup
# MAGIC
# MAGIC One-shot:
# MAGIC  1. Crea `${catalog}.gold.expense_chunks` como Delta TABLE (no materialized view)
# MAGIC     rehidratandola desde `gold.expense_audit`.
# MAGIC  2. Crea endpoint VS + Delta Sync Index con pipeline_type=TRIGGERED.
# MAGIC  3. Dispara un sync y smoke test.

# COMMAND ----------
# MAGIC %pip install databricks-vectorsearch
# MAGIC dbutils.library.restartPython()

# COMMAND ----------
dbutils.widgets.text("catalog", "nexus_dev")
dbutils.widgets.text("endpoint_name", "nexus-vs-dev")

catalog = dbutils.widgets.get("catalog")
endpoint_name = dbutils.widgets.get("endpoint_name")
source_table = f"{catalog}.gold.expense_chunks"
index_name = f"{catalog}.vector.expense_chunks_index"

from databricks.vector_search.client import VectorSearchClient  # type: ignore[import-not-found]

client = VectorSearchClient()

# COMMAND ----------
# MAGIC %md
# MAGIC ## 1. Materializar expense_chunks como tabla Delta

# COMMAND ----------
spark.sql(f"""
CREATE TABLE IF NOT EXISTS {source_table} (
    chunk_id STRING,
    tenant_id STRING,
    expense_id STRING,
    chunk_text STRING,
    amount DOUBLE,
    currency STRING,
    vendor STRING,
    date STRING,
    category STRING,
    approved_at TIMESTAMP
) USING DELTA
TBLPROPERTIES (delta.enableChangeDataFeed = true)
PARTITIONED BY (tenant_id)
""")

# Idempotent upgrade path for workspaces where the table was created before
# `currency` was added. ALTER fails if the column already exists, so we
# swallow that specific error.
try:
    spark.sql(f"ALTER TABLE {source_table} ADD COLUMNS (currency STRING)")  # noqa: S608
except Exception as _exc:
    if "already exists" not in str(_exc).lower():
        raise

spark.sql(f"""
INSERT OVERWRITE {source_table}
SELECT
    CONCAT(expense_id, '_main') AS chunk_id,
    tenant_id,
    expense_id,
    CONCAT_WS(' ',
        'Gasto en', final_vendor,
        'por', CAST(final_amount AS STRING), final_currency,
        'el', CAST(final_date AS STRING),
        '. Categoria:', category, '.'
    ) AS chunk_text,
    CAST(final_amount AS DOUBLE) AS amount,
    final_currency AS currency,
    final_vendor AS vendor,
    CAST(final_date AS STRING) AS date,
    category,
    approved_at
FROM {catalog}.gold.expense_audit
""")

row_count = spark.table(source_table).count()
print(f"[vector-setup] {source_table}: {row_count} rows")

# COMMAND ----------
# MAGIC %md
# MAGIC ## 2. Endpoint

# COMMAND ----------
existing_endpoints = [e["name"] for e in client.list_endpoints().get("endpoints", [])]

if endpoint_name not in existing_endpoints:
    print(f"Creating endpoint {endpoint_name}...")
    client.create_endpoint(name=endpoint_name, endpoint_type="STANDARD")
    client.wait_for_endpoint(name=endpoint_name, timeout=1200)
    print(f"Endpoint {endpoint_name} ONLINE")
else:
    print(f"Endpoint {endpoint_name} already exists")

# COMMAND ----------
# MAGIC %md
# MAGIC ## 3. Delta Sync Index

# COMMAND ----------
existing_indexes = [
    i["name"] for i in client.list_indexes(name=endpoint_name).get("vector_indexes", [])
]

if index_name not in existing_indexes:
    print(f"Creating Delta Sync index {index_name}...")
    idx = client.create_delta_sync_index(
        endpoint_name=endpoint_name,
        source_table_name=source_table,
        index_name=index_name,
        primary_key="chunk_id",
        pipeline_type="TRIGGERED",
        embedding_source_column="chunk_text",
        embedding_model_endpoint_name="databricks-bge-large-en",
    )
    print(f"Index {index_name} creado.")
else:
    idx = client.get_index(endpoint_name=endpoint_name, index_name=index_name)
    print(f"Index {index_name} already exists. Triggering sync...")
    idx.sync()
    print("Sync disparado.")

# COMMAND ----------
# MAGIC %md
# MAGIC ## 4. Smoke test

# COMMAND ----------
import time

# Esperar a que el index tenga estado READY (primera sync puede tardar ~5 min)
print("Esperando que index este READY...")
for i in range(60):
    desc = idx.describe()
    state = desc.get("status", {}).get("ready", False)
    detailed = desc.get("status", {}).get("detailed_state", "UNKNOWN")
    print(f"  [{i}] ready={state} detailed_state={detailed}")
    if state:
        break
    time.sleep(30)

sample = spark.table(source_table).select("tenant_id").distinct().limit(1).collect()
if sample:
    tenant = sample[0]["tenant_id"]
    print(f"Probando similarity_search con tenant_id={tenant}...")
    results = idx.similarity_search(
        query_text="cafe Starbucks",
        columns=["chunk_id", "expense_id", "chunk_text", "amount", "currency", "vendor", "date"],
        num_results=3,
        filters={"tenant_id": tenant},
    )
    for r in results.get("result", {}).get("data_array", []):
        print(r)
else:
    print("No hay datos en expense_chunks. Corre gold primero.")
