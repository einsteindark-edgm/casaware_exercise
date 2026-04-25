# Databricks notebook source
# MAGIC %md
# MAGIC # gold.expense_chunks
# MAGIC
# MAGIC Chunks de texto para RAG. Un chunk por expense aprobado, con:
# MAGIC  - `chunk_id` = expense_id + "_main" (estable)
# MAGIC  - `chunk_text` en espaniol, optimizado para busqueda semantica.
# MAGIC    Incluye campos canonicos (vendor/amount/date/category) MAS los
# MAGIC    extras de Textract (line items, tax, subtotal, vendor_address,
# MAGIC    etc.) cuando estan disponibles. Permite responder consultas
# MAGIC    granulares como "que impuesto pague en X?" o "cuanto pague por Y
# MAGIC    item en Z?".
# MAGIC  - metadata filtrable: tenant_id, amount, vendor, date, category
# MAGIC
# MAGIC `delta.enableChangeDataFeed` ES REQUERIDO por Vector Search Delta Sync.

# COMMAND ----------
import dlt
from pyspark.sql.functions import (
    array,
    coalesce,
    col,
    concat_ws,
    expr,
    from_json,
    lit,
    transform,
    when,
)
from pyspark.sql.types import ArrayType, StringType, StructField, StructType


# Schema del JSON `ocr_extra` (escrito por la activity Temporal
# `upsert_ocr_extraction`). Mantener en lockstep con
# `nexus-orchestration/src/nexus_orchestration/activities/textract.py::_extract_extra`.
_OCR_EXTRA_SCHEMA = StructType(
    [
        StructField(
            "summary_fields",
            ArrayType(
                StructType(
                    [
                        StructField("field", StringType()),
                        StructField("value", StringType()),
                    ]
                )
            ),
        ),
        StructField(
            "line_items",
            ArrayType(
                StructType(
                    [
                        StructField("item", StringType()),
                        StructField("price", StringType()),
                        StructField("quantity", StringType()),
                        StructField("unit_price", StringType()),
                    ]
                )
            ),
        ),
    ]
)


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
    enriched = (
        dlt.read("expense_audit")
        .withColumn("_extra", from_json(col("ocr_extra"), _OCR_EXTRA_SCHEMA))
        # Renderizamos los extras como texto plano una sola vez aqui para que
        # el chunk_text quede como un string unico embedeable. Cada item se
        # serializa "{quantity}x {item} {price}" y los summary_fields como
        # "{field}: {value}".
        .withColumn(
            "_items_text",
            when(
                col("_extra.line_items").isNotNull(),
                concat_ws(
                    ", ",
                    transform(
                        col("_extra.line_items"),
                        lambda li: concat_ws(
                            " ",
                            coalesce(li["quantity"], lit("")),
                            coalesce(li["item"], lit("")),
                            coalesce(li["price"], lit("")),
                        ),
                    ),
                ),
            ).otherwise(lit("")),
        )
        .withColumn(
            "_extras_text",
            when(
                col("_extra.summary_fields").isNotNull(),
                concat_ws(
                    ", ",
                    transform(
                        col("_extra.summary_fields"),
                        lambda f: concat_ws(": ", f["field"], f["value"]),
                    ),
                ),
            ).otherwise(lit("")),
        )
    )

    return enriched.select(
        concat_ws("", col("expense_id"), lit("_main")).alias("chunk_id"),
        col("tenant_id"),
        col("expense_id"),
        # Frase canonica + secciones opcionales. concat_ws ignora valores NULL
        # pero NO ignora strings vacios — por eso usamos `when(...).otherwise`
        # arriba para colapsar a "" cuando no hay extras, y luego filtramos
        # con un CASE: solo agregamos la seccion si tiene contenido.
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
            when(col("_items_text") != "", concat_ws(" ", lit("Items:"), col("_items_text"), lit("."))).otherwise(lit("")),
            when(col("_extras_text") != "", concat_ws(" ", lit("Detalles:"), col("_extras_text"), lit("."))).otherwise(lit("")),
        ).alias("chunk_text"),
        col("final_amount").cast("double").alias("amount"),
        col("final_currency").alias("currency"),
        col("final_vendor").alias("vendor"),
        col("final_date").cast("string").alias("date"),
        col("category"),
        col("approved_at"),
    )
