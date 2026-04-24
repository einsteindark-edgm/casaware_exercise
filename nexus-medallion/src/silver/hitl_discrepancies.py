# Databricks notebook source
# MAGIC %md
# MAGIC # silver.hitl_discrepancies
# MAGIC
# MAGIC Una fila por (task_id, field_in_conflict). Explode del array
# MAGIC `discrepancy_fields` de silver.hitl_events para facilitar BI queries:
# MAGIC "¿qué campo entra más seguido en conflicto?", "¿cuántos conflictos
# MAGIC aprobados vs rechazados por campo?".
# MAGIC
# MAGIC **NOTA:** materialized view (no streaming) porque su fuente
# MAGIC `silver.hitl_events` se escribe con apply_changes (SCD1/MERGE).
# MAGIC Un streaming source de Delta falla con DELTA_SOURCE_TABLE_IGNORE_CHANGES
# MAGIC al detectar updates — mismo patrón que `gold.expense_audit`.

# COMMAND ----------
import dlt
from pyspark.sql.functions import col, explode


@dlt.table(
    name="hitl_discrepancies",
    comment="Un conflicto por fila — derivado de silver.hitl_events.discrepancy_fields.",
    table_properties={
        "quality": "silver",
        "delta.enableChangeDataFeed": "true",
    },
)
def hitl_discrepancies():
    return (
        dlt.read("hitl_events")
        .where("discrepancy_fields IS NOT NULL AND size(discrepancy_fields) > 0")
        .select(
            col("task_id"),
            col("tenant_id"),
            col("expense_id"),
            col("workflow_id"),
            explode(col("discrepancy_fields")).alias("field_name"),
            col("status"),
            col("decision"),
            col("resolved_by"),
            col("resolved_at"),
            col("created_at"),
        )
    )
