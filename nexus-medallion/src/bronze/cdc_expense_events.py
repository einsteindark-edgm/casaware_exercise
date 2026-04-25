# Databricks notebook source
# MAGIC %md
# MAGIC # bronze.mongodb_cdc_expense_events (Kafka source — Phase B+)

# COMMAND ----------
import dlt
from pyspark.sql.functions import col, current_timestamp, from_json
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
    comment="CDC events desde topic Kafka nexus.nexus_dev.expense_events (Debezium).",
    table_properties={
        "delta.enableChangeDataFeed": "true",
        "quality": "bronze",
        "pipelines.reset.allowed": "false",
    },
)
def mongodb_cdc_expense_events():
    return (
        spark.readStream.format("kafka")
        .option("kafka.bootstrap.servers", spark.conf.get("nexus.msk_bootstrap"))
        .option("subscribe", "nexus.nexus_dev.expense_events")
        .option("startingOffsets", "latest")
        .option("failOnDataLoss", "false")
        .option(
            "databricks.serviceCredential",
            spark.conf.get("nexus.msk_service_credential"),
        )
        .load()
        .filter(col("value").isNotNull())
        .select(from_json(col("value").cast("string"), EVENTS_JSON_SCHEMA).alias("p"))
        .select(
            col("p.event_id").alias("event_id"),
            col("p.expense_id").alias("expense_id"),
            col("p.tenant_id").alias("tenant_id"),
            col("p.event_type").alias("event_type"),
            col("p.actor").alias("actor"),
            col("p.details").alias("details"),
            col("p.workflow_id").alias("workflow_id"),
            col("p.created_at").cast("timestamp").alias("created_at"),
            col("p.__op").alias("__op"),
            col("p.__source_ts_ms").alias("__source_ts_ms"),
            col("p.__deleted").alias("__deleted"),
            current_timestamp().alias("_cdc_ingestion_ts"),
        )
    )
