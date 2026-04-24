# Databricks notebook source
# MAGIC %md
# MAGIC # bronze.mongodb_cdc_expenses (Autoloader)
# MAGIC
# MAGIC Lee eventos JSONL.gz que el listener (nexus-cdc) escribe en
# MAGIC `s3://{cdc_bucket}/expenses/...` y los apendea a la tabla bronze.
# MAGIC Mismo target que `seed_bronze_from_mongo.py` (convive en append).
# MAGIC
# MAGIC Schema explicito: evita inferencia costosa y garantiza consistencia
# MAGIC con la tabla que escribio el seed (DecimalType casteado aqui).

# COMMAND ----------
import dlt
from pyspark.sql.functions import col, current_timestamp, lit
from pyspark.sql.types import (
    BooleanType,
    DoubleType,
    LongType,
    MapType,
    StringType,
    StructField,
    StructType,
)

# JSON types only. CDC emitter converts Decimal->float, datetime->ISO.
EXPENSES_JSON_SCHEMA = StructType(
    [
        StructField("expense_id", StringType()),
        StructField("tenant_id", StringType()),
        StructField("user_id", StringType()),
        StructField("receipt_id", StringType()),
        StructField("workflow_id", StringType()),
        StructField("amount", DoubleType()),
        StructField("currency", StringType()),
        StructField("date", StringType()),
        StructField("vendor", StringType()),
        StructField("category", StringType()),
        StructField("status", StringType()),
        StructField("final_amount", DoubleType()),
        StructField("final_vendor", StringType()),
        StructField("final_date", StringType()),
        StructField("final_currency", StringType()),
        StructField("source_per_field", MapType(StringType(), StringType())),
        StructField("approved_at", StringType()),
        StructField("created_at", StringType()),
        StructField("updated_at", StringType()),
        StructField("__op", StringType()),
        StructField("__source_ts_ms", LongType()),
        StructField("__deleted", BooleanType()),
    ]
)


@dlt.table(
    name="mongodb_cdc_expenses",
    comment="CDC events desde s3://{cdc_bucket}/expenses/ via Autoloader.",
    table_properties={
        "delta.enableChangeDataFeed": "true",
        "quality": "bronze",
        "pipelines.reset.allowed": "false",
    },
)
def mongodb_cdc_expenses():
    base = f"s3://{spark.conf.get('nexus.cdc_bucket')}/expenses/"
    schema_loc = f"s3://{spark.conf.get('nexus.cdc_bucket')}/_autoloader_state/expenses/"
    return (
        spark.readStream.format("cloudFiles")
        .option("cloudFiles.format", "json")
        .option("cloudFiles.schemaLocation", schema_loc)
        .option("cloudFiles.inferColumnTypes", "false")
        .option("cloudFiles.includeExistingFiles", "true")
        .option("cloudFiles.useIncrementalListing", "auto")
        .schema(EXPENSES_JSON_SCHEMA)
        .load(base)
        .select(
            col("expense_id"),
            col("tenant_id"),
            col("user_id"),
            col("receipt_id"),
            col("workflow_id"),
            col("amount").cast("decimal(18,2)").alias("amount"),
            col("currency"),
            col("date"),
            col("vendor"),
            col("category"),
            col("status"),
            col("final_amount").cast("decimal(18,2)").alias("final_amount"),
            col("final_vendor"),
            col("final_date"),
            col("final_currency"),
            col("source_per_field"),
            col("approved_at").cast("timestamp").alias("approved_at"),
            col("created_at").cast("timestamp").alias("created_at"),
            col("updated_at").cast("timestamp").alias("updated_at"),
            col("__op"),
            col("__source_ts_ms"),
            col("__deleted"),
            current_timestamp().alias("_cdc_ingestion_ts"),
        )
    )
