# Databricks notebook source
# MAGIC %md
# MAGIC # gold.expense_chunks
# MAGIC
# MAGIC Chunks de texto para RAG. Un chunk por expense aprobado, con:
# MAGIC  - `chunk_id` = expense_id + "_main" (estable)
# MAGIC  - `chunk_text` en espaniol, optimizado para busqueda semantica
# MAGIC  - metadata filtrable: tenant_id, amount, vendor, date, category
# MAGIC
# MAGIC `delta.enableChangeDataFeed` ES REQUERIDO por Vector Search Delta Sync.

# COMMAND ----------
import dlt
from pyspark.sql.functions import col, concat, concat_ws, lit


# NOTE: this is a DLT MATERIALIZED VIEW — UPDATE/MERGE are not allowed on it.
# Therefore embeddings live in a SEPARATE managed table `gold.expense_embeddings`
# written by the Temporal activity `trigger_vector_sync`. vector_search.py
# joins both tables.
@dlt.table(
    name="expense_chunks",
    comment="Chunks de texto indexables por Mosaic AI Vector Search.",
    table_properties={
        "quality": "gold",
        "delta.enableChangeDataFeed": "true",
    },
    partition_cols=["tenant_id"],  # ayuda al filtro tenant en VS
)
def expense_chunks():
    return (
        dlt.read("expense_audit")
        .select(
            concat(col("expense_id"), lit("_main")).alias("chunk_id"),
            col("tenant_id"),
            col("expense_id"),
            concat_ws(
                " ",
                lit("Gasto en"),
                col("final_vendor"),
                lit("por"),
                col("final_amount").cast("string"),
                col("final_currency"),
                lit("el"),
                col("final_date").cast("string"),
                lit("."),
                lit("Categoria:"),
                col("category"),
                lit("."),
            ).alias("chunk_text"),
            col("final_amount").cast("double").alias("amount"),
            col("final_currency").alias("currency"),
            col("final_vendor").alias("vendor"),
            col("final_date").cast("string").alias("date"),
            col("category"),
            col("approved_at"),
        )
    )
