# Databricks notebook source
# MAGIC %md
# MAGIC # bronze.mongodb_cdc_expense_events (Autoloader)

# COMMAND ----------
import dlt
from pyspark.sql.functions import col, current_timestamp
from pyspark.sql.types import (
    BooleanType,
    LongType,
    MapType,
    StringType,
    StructField,
    StructType,
)

EVENTS_JSON_SCHEMA = StructType(
    [
        StructField("event_id", StringType()),
        StructField("expense_id", StringType()),
        StructField("tenant_id", StringType()),
        StructField("event_type", StringType()),
        StructField("actor", StringType()),
        StructField("details", MapType(StringType(), StringType())),
        StructField("workflow_id", StringType()),
        StructField("created_at", StringType()),
        StructField("__op", StringType()),
        StructField("__source_ts_ms", LongType()),
        StructField("__deleted", BooleanType()),
    ]
)


@dlt.table(
    name="mongodb_cdc_expense_events",
    comment="CDC events desde s3://{cdc_bucket}/expense_events/ via Autoloader.",
    table_properties={
        "delta.enableChangeDataFeed": "true",
        "quality": "bronze",
        "pipelines.reset.allowed": "false",
    },
)
def mongodb_cdc_expense_events():
    base = f"s3://{spark.conf.get('nexus.cdc_bucket')}/expense_events/"
    schema_loc = f"s3://{spark.conf.get('nexus.cdc_bucket')}/_autoloader_state/expense_events/"
    return (
        spark.readStream.format("cloudFiles")
        .option("cloudFiles.format", "json")
        .option("cloudFiles.schemaLocation", schema_loc)
        .option("cloudFiles.inferColumnTypes", "false")
        .option("cloudFiles.includeExistingFiles", "true")
        .option("cloudFiles.useIncrementalListing", "auto")
        .schema(EVENTS_JSON_SCHEMA)
        .load(base)
        .select(
            col("event_id"),
            col("expense_id"),
            col("tenant_id"),
            col("event_type"),
            col("actor"),
            col("details"),
            col("workflow_id"),
            col("created_at").cast("timestamp").alias("created_at"),
            col("__op"),
            col("__source_ts_ms"),
            col("__deleted"),
            current_timestamp().alias("_cdc_ingestion_ts"),
        )
    )
