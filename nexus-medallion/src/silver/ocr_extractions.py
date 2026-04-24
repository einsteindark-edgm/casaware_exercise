# Databricks notebook source
# MAGIC %md
# MAGIC # silver.ocr_extractions
# MAGIC
# MAGIC Ultimas extracciones OCR por expense. El Worker ya normaliza
# MAGIC total/vendor/date/confidences en la coleccion Mongo, asi que
# MAGIC aqui solo es SCD1 sobre expense_id.

# COMMAND ----------
import dlt
from pyspark.sql.functions import col, expr


@dlt.view(name="bronze_ocr_source")
def bronze_ocr_source():
    return (
        spark.readStream.option("ignoreChanges", "true")
        .table(f"{spark.conf.get('nexus.catalog')}.bronze.mongodb_cdc_ocr_extractions")
    )


@dlt.view
def v_ocr_cdc_cleansed():
    return (
        spark.readStream.table("LIVE.bronze_ocr_source")
        .select(
            col("expense_id"),
            col("tenant_id"),
            col("user_id"),
            col("ocr_total").cast("decimal(18,2)"),
            col("ocr_total_confidence").cast("double"),
            col("ocr_vendor"),
            col("ocr_vendor_confidence").cast("double"),
            col("ocr_date").cast("date"),
            col("ocr_date_confidence").cast("double"),
            col("ocr_currency"),
            col("avg_confidence").cast("double"),
            col("textract_raw_s3_key"),
            col("extracted_at").cast("timestamp"),
            col("__op").alias("_op"),
            col("__source_ts_ms").alias("_source_ts_ms"),
        )
        .where("expense_id IS NOT NULL")
    )


dlt.create_streaming_table(
    name="ocr_extractions",
    comment="Ultima extraccion OCR por expense (replicada de Mongo vIa CDC/seed).",
    table_properties={"quality": "silver", "delta.enableChangeDataFeed": "true"},
    expect_all={
        "valid_confidence": "avg_confidence IS NULL OR avg_confidence BETWEEN 0 AND 100",
        "has_signal": "ocr_total IS NOT NULL OR ocr_vendor IS NOT NULL",
    },
)

dlt.apply_changes(
    target="ocr_extractions",
    source="v_ocr_cdc_cleansed",
    keys=["expense_id"],
    sequence_by=col("_source_ts_ms"),
    apply_as_deletes=expr("_op = 'd'"),
    except_column_list=["_op", "_source_ts_ms"],
    stored_as_scd_type="1",
)
