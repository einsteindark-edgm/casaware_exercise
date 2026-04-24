# Databricks notebook source
# MAGIC %md
# MAGIC # bronze.mongodb_cdc_receipts (Autoloader)

# COMMAND ----------
import dlt
from pyspark.sql.functions import col, current_timestamp
from pyspark.sql.types import (
    BooleanType,
    LongType,
    StringType,
    StructField,
    StructType,
)

RECEIPTS_JSON_SCHEMA = StructType(
    [
        StructField("receipt_id", StringType()),
        StructField("tenant_id", StringType()),
        StructField("user_id", StringType()),
        StructField("expense_id", StringType()),
        StructField("s3_key", StringType()),
        StructField("content_type", StringType()),
        StructField("size_bytes", LongType()),
        StructField("uploaded_at", StringType()),
        StructField("created_at", StringType()),
        StructField("__op", StringType()),
        StructField("__source_ts_ms", LongType()),
        StructField("__deleted", BooleanType()),
    ]
)


@dlt.table(
    name="mongodb_cdc_receipts",
    comment="CDC events desde s3://{cdc_bucket}/receipts/ via Autoloader.",
    table_properties={
        "delta.enableChangeDataFeed": "true",
        "quality": "bronze",
        "pipelines.reset.allowed": "false",
    },
)
def mongodb_cdc_receipts():
    base = f"s3://{spark.conf.get('nexus.cdc_bucket')}/receipts/"
    schema_loc = f"s3://{spark.conf.get('nexus.cdc_bucket')}/_autoloader_state/receipts/"
    return (
        spark.readStream.format("cloudFiles")
        .option("cloudFiles.format", "json")
        .option("cloudFiles.schemaLocation", schema_loc)
        .option("cloudFiles.inferColumnTypes", "false")
        .option("cloudFiles.includeExistingFiles", "true")
        .option("cloudFiles.useIncrementalListing", "auto")
        .schema(RECEIPTS_JSON_SCHEMA)
        .load(base)
        .select(
            col("receipt_id"),
            col("tenant_id"),
            col("user_id"),
            col("expense_id"),
            col("s3_key"),
            col("content_type"),
            col("size_bytes"),
            col("uploaded_at").cast("timestamp").alias("uploaded_at"),
            col("__op"),
            col("__source_ts_ms"),
            col("__deleted"),
            current_timestamp().alias("_cdc_ingestion_ts"),
        )
    )
