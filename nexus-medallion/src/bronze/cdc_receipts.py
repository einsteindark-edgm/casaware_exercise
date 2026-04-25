# Databricks notebook source
# MAGIC %md
# MAGIC # bronze.mongodb_cdc_receipts (Kafka source — Phase B+)

# COMMAND ----------
import dlt
from pyspark.sql.functions import col, current_timestamp, from_json
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
    comment="CDC events desde topic Kafka nexus.nexus_dev.receipts (Debezium).",
    table_properties={
        "delta.enableChangeDataFeed": "true",
        "quality": "bronze",
        "pipelines.reset.allowed": "false",
    },
)
def mongodb_cdc_receipts():
    return (
        spark.readStream.format("kafka")
        .option("kafka.bootstrap.servers", spark.conf.get("nexus.msk_bootstrap"))
        .option("subscribe", "nexus.nexus_dev.receipts")
        .option("startingOffsets", "latest")
        .option("failOnDataLoss", "false")
        .option(
            "databricks.serviceCredential",
            spark.conf.get("nexus.msk_service_credential"),
        )
        .load()
        .filter(col("value").isNotNull())
        .select(from_json(col("value").cast("string"), RECEIPTS_JSON_SCHEMA).alias("p"))
        .select(
            col("p.receipt_id").alias("receipt_id"),
            col("p.tenant_id").alias("tenant_id"),
            col("p.user_id").alias("user_id"),
            col("p.expense_id").alias("expense_id"),
            col("p.s3_key").alias("s3_key"),
            col("p.content_type").alias("content_type"),
            col("p.size_bytes").alias("size_bytes"),
            col("p.uploaded_at").cast("timestamp").alias("uploaded_at"),
            col("p.__op").alias("__op"),
            col("p.__source_ts_ms").alias("__source_ts_ms"),
            col("p.__deleted").alias("__deleted"),
            current_timestamp().alias("_cdc_ingestion_ts"),
        )
    )
