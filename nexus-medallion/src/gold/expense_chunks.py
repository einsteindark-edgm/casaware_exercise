# Databricks notebook source
# MAGIC %md
# MAGIC # gold.expense_chunks
# MAGIC
# MAGIC Chunks de texto para RAG. Un chunk por expense aprobado, con:
# MAGIC  - `chunk_id` = expense_id + "_main" (estable)
# MAGIC  - `chunk_text` en espaniol, optimizado para busqueda semantica.
# MAGIC    Incluye campos canonicos (vendor/amount/date/category) MAS los
# MAGIC    extras de Textract (line items, tax, subtotal, vendor_address,
# MAGIC    invoice_id, payment terms, etc.) cuando estan disponibles.
# MAGIC    Permite responder consultas granulares como "que impuesto pague
# MAGIC    en X?" o "cuanto pague por Y item en Z?".
# MAGIC  - metadata filtrable: tenant_id, amount, vendor, date, category
# MAGIC
# MAGIC `delta.enableChangeDataFeed` ES REQUERIDO por Vector Search Delta Sync.
# MAGIC
# MAGIC ## Por que parseamos `ocr_extra` con DOS schemas y `coalesce`
# MAGIC
# MAGIC `_extract_extra` en la activity Textract escribe los extras a Mongo
# MAGIC como **arrays de structs**: `{"summary_fields": [{...}, {...}],
# MAGIC "line_items": [{...}]}`. Pero cuando Debezium emite el documento al
# MAGIC topic Kafka, los arrays con sub-documentos se serializan como
# MAGIC **objetos con keys `_0`, `_1`, ...** — comportamiento documentado del
# MAGIC connector MongoDB cuando array.encoding != array. El path de seed
# MAGIC (`seed_bronze_from_mongo.py`) en cambio usa `json.dumps()` y preserva
# MAGIC arrays reales.
# MAGIC
# MAGIC Resultado: en bronze tenemos AMBAS formas conviviendo. Si parseamos
# MAGIC con un solo schema, la otra mitad cae a NULL y el chunk_text pierde
# MAGIC los extras (incidente 2026-04-25 sobre exp_01KQ3BAQ6PP5YME7WH53WJQV4X).
# MAGIC
# MAGIC La solucion: dos `from_json` (uno con ArrayType, otro con MapType) +
# MAGIC `coalesce(array_form, map_values(map_form))`. El que parsee bien gana,
# MAGIC el otro queda NULL.

# COMMAND ----------
import dlt
from pyspark.sql.functions import (
    coalesce,
    col,
    concat_ws,
    from_json,
    lit,
    map_values,
    transform,
    when,
)
from pyspark.sql.types import (
    ArrayType,
    DoubleType,
    MapType,
    StringType,
    StructField,
    StructType,
)


# Sub-struct de un line_item. Mantener en lockstep con
# `nexus-orchestration/.../activities/textract.py::_extract_extra`.
_LINE_ITEM_STRUCT = StructType(
    [
        StructField("item", StringType()),
        StructField("price", StringType()),
        StructField("quantity", StringType()),
        StructField("unit_price", StringType()),
        StructField("product_code", StringType()),
        StructField("row_text", StringType()),
        StructField("confidence_avg", DoubleType()),
    ]
)

# Sub-struct de un summary_field.
_SUMMARY_FIELD_STRUCT = StructType(
    [
        StructField("field", StringType()),
        StructField("value", StringType()),
        StructField("confidence", DoubleType()),
        StructField("label_text", StringType()),
    ]
)

# Forma "array" — esperada del path de seed (json.dumps con listas Python).
_OCR_EXTRA_AS_ARRAYS = StructType(
    [
        StructField("summary_fields", ArrayType(_SUMMARY_FIELD_STRUCT)),
        StructField("line_items", ArrayType(_LINE_ITEM_STRUCT)),
    ]
)

# Forma "map" — esperada del path Debezium (arrays serializados como
# objetos con keys `_0`, `_1`, ...).
_OCR_EXTRA_AS_MAPS = StructType(
    [
        StructField(
            "summary_fields", MapType(StringType(), _SUMMARY_FIELD_STRUCT)
        ),
        StructField("line_items", MapType(StringType(), _LINE_ITEM_STRUCT)),
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
        # Parseamos en ambas formas. La que matchee la real gana via coalesce.
        .withColumn("_extra_arr", from_json(col("ocr_extra"), _OCR_EXTRA_AS_ARRAYS))
        .withColumn("_extra_map", from_json(col("ocr_extra"), _OCR_EXTRA_AS_MAPS))
        # Normalizamos a array<struct> sin importar de donde vino.
        .withColumn(
            "_line_items",
            coalesce(
                col("_extra_arr.line_items"),
                map_values(col("_extra_map.line_items")),
            ),
        )
        .withColumn(
            "_summary_fields",
            coalesce(
                col("_extra_arr.summary_fields"),
                map_values(col("_extra_map.summary_fields")),
            ),
        )
        # Renderizamos line_items: "{quantity}x {item} {unit_price} = {price}"
        # con coalesce a "" para que concat_ws no inyecte NULLs raros, y
        # luego envolvemos cada item entre comas/separador.
        .withColumn(
            "_items_text",
            when(
                col("_line_items").isNotNull(),
                concat_ws(
                    "; ",
                    transform(
                        col("_line_items"),
                        lambda li: concat_ws(
                            " ",
                            coalesce(li["quantity"], lit("")),
                            coalesce(li["item"], lit("")),
                            # unit_price entre parentesis si existe
                            when(
                                li["unit_price"].isNotNull(),
                                concat_ws("", lit("@"), li["unit_price"]),
                            ).otherwise(lit("")),
                            coalesce(li["price"], lit("")),
                        ),
                    ),
                ),
            ).otherwise(lit("")),
        )
        # Renderizamos summary_fields como "{field}: {value}". El nombre del
        # field viene normalizado (subtotal, tax, vendor_address, etc.).
        .withColumn(
            "_extras_text",
            when(
                col("_summary_fields").isNotNull(),
                concat_ws(
                    "; ",
                    transform(
                        col("_summary_fields"),
                        lambda f: concat_ws(
                            ": ",
                            coalesce(f["field"], lit("")),
                            coalesce(f["value"], lit("")),
                        ),
                    ),
                ),
            ).otherwise(lit("")),
        )
    )

    return enriched.select(
        concat_ws("", col("expense_id"), lit("_main")).alias("chunk_id"),
        col("tenant_id"),
        col("expense_id"),
        # Frase canonica + secciones opcionales. concat_ws ignora NULL pero
        # NO strings vacios — usamos `when(...).otherwise("")` arriba para
        # colapsar a "" cuando no hay extras, y luego el CASE de abajo solo
        # agrega la seccion si tiene contenido.
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
            when(
                col("_items_text") != "",
                concat_ws(" ", lit("Items:"), col("_items_text"), lit(".")),
            ).otherwise(lit("")),
            when(
                col("_extras_text") != "",
                concat_ws(" ", lit("Detalles:"), col("_extras_text"), lit(".")),
            ).otherwise(lit("")),
        ).alias("chunk_text"),
        col("final_amount").cast("double").alias("amount"),
        col("final_currency").alias("currency"),
        col("final_vendor").alias("vendor"),
        col("final_date").cast("string").alias("date"),
        col("category"),
        col("approved_at"),
    )
