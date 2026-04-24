# Databricks notebook source
# MAGIC %md
# MAGIC # bronze.mongodb_cdc_ocr_extractions (Autoloader)

# COMMAND ----------
import dlt
from pyspark.sql.functions import col, current_timestamp
from pyspark.sql.types import (
    BooleanType,
    DoubleType,
    LongType,
    StringType,
    StructField,
    StructType,
)

OCR_JSON_SCHEMA = StructType(
    [
        StructField("expense_id", StringType()),
        StructField("tenant_id", StringType()),
        StructField("user_id", StringType()),
        StructField("ocr_total", DoubleType()),
        StructField("ocr_total_confidence", DoubleType()),
        StructField("ocr_vendor", StringType()),
        StructField("ocr_vendor_confidence", DoubleType()),
        StructField("ocr_date", StringType()),
        StructField("ocr_date_confidence", DoubleType()),
        StructField("ocr_currency", StringType()),
        StructField("avg_confidence", DoubleType()),
        StructField("textract_raw_s3_key", StringType()),
        StructField("extracted_at", StringType()),
        StructField("created_at", StringType()),
        StructField("__op", StringType()),
        StructField("__source_ts_ms", LongType()),
        StructField("__deleted", BooleanType()),
    ]
)


@dlt.table(
    name="mongodb_cdc_ocr_extractions",
    comment="CDC events desde s3://{cdc_bucket}/ocr_extractions/ via Autoloader.",
    table_properties={
        "delta.enableChangeDataFeed": "true",
        "quality": "bronze",
        "pipelines.reset.allowed": "false",
    },
)
def mongodb_cdc_ocr_extractions():
    base = f"s3://{spark.conf.get('nexus.cdc_bucket')}/ocr_extractions/"
    schema_loc = f"s3://{spark.conf.get('nexus.cdc_bucket')}/_autoloader_state/ocr_extractions/"
    return (
        spark.readStream.format("cloudFiles")
        .option("cloudFiles.format", "json")
        .option("cloudFiles.schemaLocation", schema_loc)
        .option("cloudFiles.inferColumnTypes", "false")
        .option("cloudFiles.includeExistingFiles", "true")
        .option("cloudFiles.useIncrementalListing", "auto")
        .schema(OCR_JSON_SCHEMA)
        .load(base)
        .select(
            col("expense_id"),
            col("tenant_id"),
            col("user_id"),
            col("ocr_total").cast("decimal(18,2)").alias("ocr_total"),
            col("ocr_total_confidence"),
            col("ocr_vendor"),
            col("ocr_vendor_confidence"),
            col("ocr_date"),
            col("ocr_date_confidence"),
            col("ocr_currency"),
            col("avg_confidence"),
            col("textract_raw_s3_key"),
            col("extracted_at").cast("timestamp").alias("extracted_at"),
            col("__op"),
            col("__source_ts_ms"),
            col("__deleted"),
            current_timestamp().alias("_cdc_ingestion_ts"),
        )
    )
