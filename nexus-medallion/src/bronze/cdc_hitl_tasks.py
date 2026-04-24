# Databricks notebook source
# MAGIC %md
# MAGIC # bronze.mongodb_cdc_hitl_tasks (Autoloader)

# COMMAND ----------
import dlt
from pyspark.sql.functions import col, current_timestamp
from pyspark.sql.types import (
    ArrayType,
    BooleanType,
    LongType,
    MapType,
    StringType,
    StructField,
    StructType,
)

HITL_JSON_SCHEMA = StructType(
    [
        StructField("task_id", StringType()),
        StructField("tenant_id", StringType()),
        StructField("expense_id", StringType()),
        StructField("workflow_id", StringType()),
        StructField("status", StringType()),
        StructField("discrepancy_fields", ArrayType(StringType())),
        StructField("decision", StringType()),
        StructField("resolved_fields", MapType(StringType(), StringType())),
        StructField("resolved_by", StringType()),
        StructField("resolved_at", StringType()),
        StructField("created_at", StringType()),
        StructField("__op", StringType()),
        StructField("__source_ts_ms", LongType()),
        StructField("__deleted", BooleanType()),
    ]
)


@dlt.table(
    name="mongodb_cdc_hitl_tasks",
    comment="CDC events desde s3://{cdc_bucket}/hitl_tasks/ via Autoloader.",
    table_properties={
        "delta.enableChangeDataFeed": "true",
        "quality": "bronze",
        "pipelines.reset.allowed": "false",
    },
)
def mongodb_cdc_hitl_tasks():
    base = f"s3://{spark.conf.get('nexus.cdc_bucket')}/hitl_tasks/"
    schema_loc = f"s3://{spark.conf.get('nexus.cdc_bucket')}/_autoloader_state/hitl_tasks/"
    return (
        spark.readStream.format("cloudFiles")
        .option("cloudFiles.format", "json")
        .option("cloudFiles.schemaLocation", schema_loc)
        .option("cloudFiles.inferColumnTypes", "false")
        .option("cloudFiles.includeExistingFiles", "true")
        .option("cloudFiles.useIncrementalListing", "auto")
        .schema(HITL_JSON_SCHEMA)
        .load(base)
        .select(
            col("task_id"),
            col("tenant_id"),
            col("expense_id"),
            col("workflow_id"),
            col("status"),
            col("discrepancy_fields"),
            col("decision"),
            col("resolved_fields"),
            col("resolved_by"),
            col("resolved_at").cast("timestamp").alias("resolved_at"),
            col("created_at").cast("timestamp").alias("created_at"),
            col("__op"),
            col("__source_ts_ms"),
            col("__deleted"),
            current_timestamp().alias("_cdc_ingestion_ts"),
        )
    )
