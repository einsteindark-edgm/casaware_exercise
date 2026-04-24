# Databricks notebook source
# MAGIC %md
# MAGIC # gold.expense_audit
# MAGIC
# MAGIC Promocion reactiva: cuando `silver.expenses.status = 'approved'`,
# MAGIC materializa una fila aqui. Todos los campos `final_*` ya vienen
# MAGIC embebidos desde el Worker (no hay joins de enriquecimiento en Gold).

# COMMAND ----------
import dlt
from pyspark.sql.functions import coalesce, col


@dlt.view
def v_approved_expenses():
    return (
        dlt.read_stream("silver.expenses")
        .where("status = 'approved'")
        .select(
            "tenant_id",
            "expense_id",
            "user_id",
            "receipt_id",
            # final_* preferido (lo escribe el Worker al aprobar); si no
            # existe porque vino de un flujo sin HITL, cae al campo user-reported.
            coalesce(col("final_amount"), col("amount")).alias("final_amount"),
            coalesce(col("final_vendor"), col("vendor")).alias("final_vendor"),
            coalesce(col("final_date"), col("date")).alias("final_date"),
            coalesce(col("final_currency"), col("currency")).alias("final_currency"),
            "category",
            "source_per_field",
            coalesce(col("approved_at"), col("updated_at")).alias("approved_at"),
        )
    )


dlt.create_streaming_table(
    name="expense_audit",
    comment="Un registro por expense aprobado. BI, RAG y reportes consumen desde aqui.",
    table_properties={
        "quality": "gold",
        "delta.enableChangeDataFeed": "true",
    },
    cluster_by=["tenant_id", "final_date"],
)

dlt.apply_changes(
    target="expense_audit",
    source="v_approved_expenses",
    keys=["expense_id"],
    sequence_by=col("approved_at"),
    stored_as_scd_type="1",
)
