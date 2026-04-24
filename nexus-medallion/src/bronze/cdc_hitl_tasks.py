# Databricks notebook source
# MAGIC %md
# MAGIC # bronze.mongodb_cdc_hitl_tasks (Kafka source — Phase B+)

# COMMAND ----------
import dlt
from pyspark.sql.functions import col, current_timestamp, from_json
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
    comment="CDC events desde topic Kafka nexus.nexus_dev.hitl_tasks (Debezium).",
    table_properties={
        "delta.enableChangeDataFeed": "true",
        "quality": "bronze",
        "pipelines.reset.allowed": "false",
    },
)
def mongodb_cdc_hitl_tasks():
    return (
        spark.readStream.format("kafka")
        .option("kafka.bootstrap.servers", spark.conf.get("nexus.msk_bootstrap"))
        .option("subscribe", "nexus.nexus_dev.hitl_tasks")
        .option("startingOffsets", "latest")
        .option("failOnDataLoss", "false")
        .option("kafka.security.protocol", "SASL_SSL")
        .option("kafka.sasl.mechanism", "AWS_MSK_IAM")
        .option(
            "kafka.sasl.jaas.config",
            "shadedmskiam.software.amazon.msk.auth.iam.IAMLoginModule required;",
        )
        .option(
            "kafka.sasl.client.callback.handler.class",
            "shadedmskiam.software.amazon.msk.auth.iam.IAMClientCallbackHandler",
        )
        .load()
        .filter(col("value").isNotNull())
        .select(from_json(col("value").cast("string"), HITL_JSON_SCHEMA).alias("p"))
        .select(
            col("p.task_id").alias("task_id"),
            col("p.tenant_id").alias("tenant_id"),
            col("p.expense_id").alias("expense_id"),
            col("p.workflow_id").alias("workflow_id"),
            col("p.status").alias("status"),
            col("p.discrepancy_fields").alias("discrepancy_fields"),
            col("p.decision").alias("decision"),
            col("p.resolved_fields").alias("resolved_fields"),
            col("p.resolved_by").alias("resolved_by"),
            col("p.resolved_at").cast("timestamp").alias("resolved_at"),
            col("p.created_at").cast("timestamp").alias("created_at"),
            col("p.__op").alias("__op"),
            col("p.__source_ts_ms").alias("__source_ts_ms"),
            col("p.__deleted").alias("__deleted"),
            current_timestamp().alias("_cdc_ingestion_ts"),
        )
    )
