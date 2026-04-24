# Databricks notebook source
# MAGIC %md
# MAGIC # gold.expense_audit
# MAGIC
# MAGIC Promocion reactiva: cuando `silver.expenses.status = 'approved'`,
# MAGIC materializa una fila aqui. Todos los campos `final_*` ya vienen
# MAGIC embebidos desde el Worker (no hay joins de enriquecimiento en Gold).
# MAGIC
# MAGIC **NOTA:** este nodo es un materialized view (no streaming) porque su
# MAGIC fuente `silver.expenses` se escribe con `apply_changes` (MERGE/SCD1).
# MAGIC Un streaming source de Delta falla con DELTA_SOURCE_TABLE_IGNORE_CHANGES
# MAGIC al detectar updates — lo comprobamos en prod en abril 2026: 5 updates
# MAGIC seguidos del pipeline gold fallaron con ese mismo error. La opción
# MAGIC `skipChangeCommits=true` NO sirve porque el evento que promueve a gold
# MAGIC es justamente el update a status=approved.

# COMMAND ----------
import dlt
from pyspark.sql.functions import coalesce, col


@dlt.table(
    name="expense_audit",
    comment="Un registro por expense aprobado. BI, RAG y reportes consumen desde aqui.",
    table_properties={
        "quality": "gold",
        "delta.enableChangeDataFeed": "true",
    },
    cluster_by=["tenant_id", "final_date"],
)
def expense_audit():
    return (
        dlt.read("silver.expenses")
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
