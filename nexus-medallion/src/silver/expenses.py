# Databricks notebook source
# MAGIC %md
# MAGIC # silver.expenses
# MAGIC
# MAGIC Replica viva de `expenses` desde bronze (seed o CDC). SCD1:
# MAGIC una fila por `expense_id`. El Worker embebe `final_amount`,
# MAGIC `final_vendor`, etc. al aprobar, por lo que silver.expenses ya
# MAGIC tiene todo lo que necesita `gold.expense_audit` sin joins extra.

# COMMAND ----------
import dlt
from pyspark.sql.functions import col, expr


@dlt.view
def v_expenses_cdc_cleansed():
    return (
        spark.readStream.table("LIVE.bronze_expenses_source")
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
            col("final_amount").cast("decimal(18,2)"),
            col("final_vendor"),
            col("final_date").cast("date"),
            col("final_currency"),
            col("source_per_field"),
            col("approved_at").cast("timestamp"),
            col("created_at").cast("timestamp"),
            col("updated_at").cast("timestamp"),
            col("__op").alias("_op"),
            col("__source_ts_ms").alias("_source_ts_ms"),
            col("__deleted").cast("boolean").alias("_deleted"),
        )
        .where("expense_id IS NOT NULL AND tenant_id IS NOT NULL")
    )


# Puente que hace legible bronze como streaming source para DLT
# (DLT en UC requiere referenciar como LIVE.* dentro del mismo pipeline).
@dlt.view(name="bronze_expenses_source")
def bronze_expenses_source():
    # read_stream + ignoreChanges permite releer bronze cuando el seed
    # rescribe el overwrite. En CDC real (Phase B), append-only.
    return (
        spark.readStream.option("ignoreChanges", "true")
        .table(f"{spark.conf.get('nexus.catalog')}.bronze.mongodb_cdc_expenses")
    )


dlt.create_streaming_table(
    name="expenses",
    comment="Foto viva de expenses por tenant. SCD1 keyed by expense_id.",
    table_properties={
        "quality": "silver",
        "delta.enableChangeDataFeed": "true",
    },
    expect_all={
        "valid_amount": "amount IS NULL OR (amount > 0 AND amount < 1e9)",
        "valid_tenant": "tenant_id IS NOT NULL",
    },
    expect_all_or_drop={
        "valid_status": (
            "status IN ('pending','processing','hitl_required',"
            "'approved','rejected','cancelled','failed')"
        ),
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
