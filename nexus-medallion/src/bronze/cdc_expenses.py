# Databricks notebook source
# MAGIC %md
# MAGIC # bronze.mongodb_cdc_expenses (Kafka source — Phase B+)
# MAGIC
# MAGIC Lee eventos del topic `nexus.nexus_dev.expenses` publicados por
# MAGIC Debezium Server desde MongoDB change streams. El envelope
# MAGIC `{campos..., __op, __source_ts_ms, __deleted}` lo produce el SMT
# MAGIC `ExtractNewDocumentState + add.fields=op,source.ts_ms` de Debezium,
# MAGIC idéntico al que emitía el listener Python de Phase B.
# MAGIC
# MAGIC Auth: IAM SASL via UC Service Credential configurada a nivel de
# MAGIC pipeline (`credential.nexus-dev-edgm-msk-cred`). No hay jaas.config
# MAGIC inline — DBR 16.1+ con UC Service Credentials firma automáticamente.

# COMMAND ----------
import dlt
from pyspark.sql.functions import col, current_timestamp, from_json
from pyspark.sql.types import (
    BooleanType,
    DoubleType,
    LongType,
    MapType,
    StringType,
    StructField,
    StructType,
)

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
    comment="CDC events desde topic Kafka nexus.nexus_dev.expenses (Debezium).",
    table_properties={
        "delta.enableChangeDataFeed": "true",
        "quality": "bronze",
        "pipelines.reset.allowed": "false",
    },
)
def mongodb_cdc_expenses():
    return (
        spark.readStream.format("kafka")
        .option("kafka.bootstrap.servers", spark.conf.get("nexus.msk_bootstrap"))
        .option("subscribe", "nexus.nexus_dev.expenses")
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
        .select(from_json(col("value").cast("string"), EXPENSES_JSON_SCHEMA).alias("p"))
        .select(
            col("p.expense_id").alias("expense_id"),
            col("p.tenant_id").alias("tenant_id"),
            col("p.user_id").alias("user_id"),
            col("p.receipt_id").alias("receipt_id"),
            col("p.workflow_id").alias("workflow_id"),
            col("p.amount").cast("decimal(18,2)").alias("amount"),
            col("p.currency").alias("currency"),
            col("p.date").alias("date"),
            col("p.vendor").alias("vendor"),
            col("p.category").alias("category"),
            col("p.status").alias("status"),
            col("p.final_amount").cast("decimal(18,2)").alias("final_amount"),
            col("p.final_vendor").alias("final_vendor"),
            col("p.final_date").alias("final_date"),
            col("p.final_currency").alias("final_currency"),
            col("p.source_per_field").alias("source_per_field"),
            col("p.approved_at").cast("timestamp").alias("approved_at"),
            col("p.created_at").cast("timestamp").alias("created_at"),
            col("p.updated_at").cast("timestamp").alias("updated_at"),
            col("p.__op").alias("__op"),
            col("p.__source_ts_ms").alias("__source_ts_ms"),
            col("p.__deleted").alias("__deleted"),
            current_timestamp().alias("_cdc_ingestion_ts"),
        )
    )
