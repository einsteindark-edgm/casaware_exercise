# Databricks notebook source
# MAGIC %md
# MAGIC # bronze.mongodb_cdc_ocr_extractions (Kafka source — Phase B+)

# COMMAND ----------
import dlt
from pyspark.sql.functions import col, current_timestamp, from_json
from pyspark.sql.types import (
    BooleanType,
    DoubleType,
    LongType,
    StringType,
    StructField,
    StructType,
)

# NOTE: `ocr_extra` is a JSON sub-document in Mongo (summary_fields[],
# line_items[]). Debezium emits it as a JSON string when forwarded over
# Kafka because the topic value is a single JSON envelope. We carry it
# through bronze as STRING and parse on demand in `gold.expense_chunks`
# to compose enriched chunk_text for vector search. Keeping it opaque
# here means schema changes inside ocr_extra (new line-item columns,
# new SummaryField types) don't require a bronze backfill.
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
        StructField("ocr_extra", StringType()),
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
    comment="CDC events desde topic Kafka nexus.nexus_dev.ocr_extractions (Debezium).",
    table_properties={
        "delta.enableChangeDataFeed": "true",
        "quality": "bronze",
        "pipelines.reset.allowed": "false",
    },
)
def mongodb_cdc_ocr_extractions():
    return (
        spark.readStream.format("kafka")
        .option("kafka.bootstrap.servers", spark.conf.get("nexus.msk_bootstrap"))
        .option("subscribe", "nexus.nexus_dev.ocr_extractions")
        .option("startingOffsets", "latest")
        .option("failOnDataLoss", "false")
        .option(
            "databricks.serviceCredential",
            spark.conf.get("nexus.msk_service_credential"),
        )
        .load()
        .filter(col("value").isNotNull())
        .select(from_json(col("value").cast("string"), OCR_JSON_SCHEMA).alias("p"))
        .select(
            col("p.expense_id").alias("expense_id"),
            col("p.tenant_id").alias("tenant_id"),
            col("p.user_id").alias("user_id"),
            col("p.ocr_total").cast("decimal(18,2)").alias("ocr_total"),
            col("p.ocr_total_confidence").alias("ocr_total_confidence"),
            col("p.ocr_vendor").alias("ocr_vendor"),
            col("p.ocr_vendor_confidence").alias("ocr_vendor_confidence"),
            col("p.ocr_date").alias("ocr_date"),
            col("p.ocr_date_confidence").alias("ocr_date_confidence"),
            col("p.ocr_currency").alias("ocr_currency"),
            col("p.avg_confidence").alias("avg_confidence"),
            col("p.ocr_extra").alias("ocr_extra"),
            col("p.textract_raw_s3_key").alias("textract_raw_s3_key"),
            col("p.extracted_at").cast("timestamp").alias("extracted_at"),
            col("p.__op").alias("__op"),
            col("p.__source_ts_ms").alias("__source_ts_ms"),
            col("p.__deleted").alias("__deleted"),
            current_timestamp().alias("_cdc_ingestion_ts"),
        )
    )
