# Databricks notebook source
# MAGIC %md
# MAGIC # gold.expense_audit
# MAGIC
# MAGIC Promocion reactiva: cuando `silver.expenses.status = 'approved'`,
# MAGIC materializa una fila aqui. Ahora enriquecida con:
# MAGIC  - `silver.ocr_extractions` — fallback de final_* si el worker no
# MAGIC    llegó a escribir final_data (p. ej. expenses aprobados fuera del
# MAGIC    flujo HITL normal donde el OCR era la única fuente confiable).
# MAGIC  - `silver.hitl_events` — metadata de la decisión humana: `decision`,
# MAGIC    `resolved_by`, `resolved_at` y los campos que entraron en conflicto.
# MAGIC
# MAGIC Precedencia de los campos finales (de más a menos autoritativo):
# MAGIC     final_* (corrección humana, worker escribe en HITL approved)
# MAGIC     ocr_*   (extracción Textract si el worker nunca promovió)
# MAGIC     valor user-reported (último fallback)
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
from pyspark.sql.functions import coalesce, col, max as spark_max


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
    catalog = spark.conf.get("nexus.catalog")

    expenses = dlt.read("silver.expenses").where("status = 'approved'")

    # OCR enrichment: una fila por expense_id.
    ocr = spark.read.table(f"{catalog}.silver.ocr_extractions").select(
        col("expense_id"),
        col("ocr_total"),
        col("ocr_vendor"),
        col("ocr_date"),
        col("ocr_currency"),
    )

    # HITL enrichment: agrupamos por expense_id porque en teoría un mismo
    # expense podría tener más de un HITL task; nos quedamos con el más
    # reciente via max(resolved_at). Si nunca pasó por HITL, todas las
    # columnas hitl_* quedan NULL (left join).
    hitl = (
        spark.read.table(f"{catalog}.silver.hitl_events")
        .where("status = 'resolved'")
        .groupBy("expense_id")
        .agg(
            spark_max("resolved_at").alias("hitl_resolved_at"),
            spark_max("decision").alias("hitl_decision"),
            spark_max("resolved_by").alias("hitl_resolved_by"),
            spark_max("discrepancy_fields").alias("hitl_conflict_fields"),
        )
    )

    return (
        expenses.join(ocr, on="expense_id", how="left")
        .join(hitl, on="expense_id", how="left")
        .select(
            col("tenant_id"),
            col("expense_id"),
            col("user_id"),
            col("receipt_id"),
            # Precedencia: humano > OCR > user-reported.
            coalesce(col("final_amount"), col("ocr_total"), col("amount")).alias("final_amount"),
            coalesce(col("final_vendor"), col("ocr_vendor"), col("vendor")).alias("final_vendor"),
            coalesce(col("final_date"), col("ocr_date"), col("date")).alias("final_date"),
            coalesce(col("final_currency"), col("ocr_currency"), col("currency")).alias("final_currency"),
            col("category"),
            col("source_per_field"),
            coalesce(col("approved_at"), col("updated_at")).alias("approved_at"),
            # HITL metadata — NULL si el expense nunca pasó por un task humano.
            col("hitl_decision").isNotNull().alias("had_hitl"),
            col("hitl_decision"),
            col("hitl_conflict_fields"),
            col("hitl_resolved_by"),
            col("hitl_resolved_at"),
        )
    )
