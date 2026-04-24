# Databricks notebook source
# MAGIC %md
# MAGIC # silver.expense_events
# MAGIC
# MAGIC Timeline append-only de eventos por expense. La key es event_id (ULID unico),
# MAGIC NO expense_id — puede haber multiples eventos por el mismo expense.

# COMMAND ----------
import dlt
from pyspark.sql.functions import col


@dlt.view(name="bronze_events_source")
def bronze_events_source():
    return (
        spark.readStream.option("ignoreChanges", "true")
        .table(f"{spark.conf.get('nexus.catalog')}.bronze.mongodb_cdc_expense_events")
    )


@dlt.view
def v_events_cdc_cleansed():
    return (
        spark.readStream.table("LIVE.bronze_events_source")
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
    comment="Timeline de eventos. Append-only en la practica (eventos no se mutan).",
    table_properties={"quality": "silver"},
)

dlt.apply_changes(
    target="expense_events",
    source="v_events_cdc_cleansed",
    keys=["event_id"],
    sequence_by=col("_source_ts_ms"),
    except_column_list=["_op", "_source_ts_ms"],
    stored_as_scd_type="1",
)
