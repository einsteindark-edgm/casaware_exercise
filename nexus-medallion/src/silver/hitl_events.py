# Databricks notebook source
# MAGIC %md
# MAGIC # silver.hitl_events
# MAGIC
# MAGIC Historia de tareas HITL. Una fila por task_id; cuando un task se resuelve,
# MAGIC SCD1 actualiza la misma fila con status, decision y resolved_fields.

# COMMAND ----------
import dlt
from pyspark.sql.functions import col, expr


@dlt.view(name="bronze_hitl_source")
def bronze_hitl_source():
    return (
        spark.readStream.option("ignoreChanges", "true")
        .table(f"{spark.conf.get('nexus.catalog')}.bronze.mongodb_cdc_hitl_tasks")
    )


@dlt.view
def v_hitl_cdc_cleansed():
    return (
        spark.readStream.table("LIVE.bronze_hitl_source")
        .select(
            col("task_id"),
            col("tenant_id"),
            col("expense_id"),
            col("workflow_id"),
            col("status"),
            col("discrepancy_fields"),
            col("decision"),
            col("resolved_fields"),
            col("resolved_by"),
            col("resolved_at").cast("timestamp"),
            col("created_at").cast("timestamp"),
            col("__op").alias("_op"),
            col("__source_ts_ms").alias("_source_ts_ms"),
        )
        .where("task_id IS NOT NULL")
    )


dlt.create_streaming_table(
    name="hitl_events",
    comment="HITL tasks con su resolucion (aprobar/rechazar/editar).",
    table_properties={"quality": "silver"},
)

dlt.apply_changes(
    target="hitl_events",
    source="v_hitl_cdc_cleansed",
    keys=["task_id"],
    sequence_by=col("_source_ts_ms"),
    apply_as_deletes=expr("_op = 'd'"),
    except_column_list=["_op", "_source_ts_ms"],
    stored_as_scd_type="1",
)
