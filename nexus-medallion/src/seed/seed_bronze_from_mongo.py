# Databricks notebook source
# MAGIC %md
# MAGIC # Seed bronze tables desde MongoDB Atlas
# MAGIC
# MAGIC One-shot. Lee 5 colecciones de Mongo Atlas y las escribe como
# MAGIC tablas Delta en `${catalog}.bronze.mongodb_cdc_*`.

# COMMAND ----------
# MAGIC %pip install pymongo==4.7.2
# MAGIC dbutils.library.restartPython()

# COMMAND ----------
# restartPython wipes Python state — re-read widgets and secrets here.
dbutils.widgets.text("catalog", "nexus_dev")
dbutils.widgets.text("mongodb_uri_secret_scope", "nexus")
dbutils.widgets.text("mongodb_db", "nexus_dev")

catalog = dbutils.widgets.get("catalog")
scope = dbutils.widgets.get("mongodb_uri_secret_scope")
mongo_db = dbutils.widgets.get("mongodb_db")
mongo_uri = dbutils.secrets.get(scope=scope, key="mongodb_uri")

# COMMAND ----------
from datetime import datetime, timezone
from decimal import Decimal

from pymongo import MongoClient
from pyspark.sql import Row
from pyspark.sql.types import (
    ArrayType,
    BooleanType,
    DecimalType,
    DoubleType,
    LongType,
    MapType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)


def _cdc_columns():
    return [
        StructField("__op", StringType(), nullable=False),
        StructField("__source_ts_ms", LongType(), nullable=False),
        StructField("__deleted", BooleanType(), nullable=False),
    ]


EXPENSES_SCHEMA = StructType(
    [
        StructField("expense_id", StringType(), False),
        StructField("tenant_id", StringType(), False),
        StructField("user_id", StringType(), False),
        StructField("receipt_id", StringType(), True),
        StructField("workflow_id", StringType(), True),
        StructField("amount", DecimalType(18, 2), False),
        StructField("currency", StringType(), False),
        StructField("date", StringType(), True),
        StructField("vendor", StringType(), True),
        StructField("category", StringType(), True),
        StructField("status", StringType(), False),
        StructField("final_amount", DecimalType(18, 2), True),
        StructField("final_vendor", StringType(), True),
        StructField("final_date", StringType(), True),
        StructField("final_currency", StringType(), True),
        StructField("source_per_field", MapType(StringType(), StringType()), True),
        StructField("approved_at", TimestampType(), True),
        StructField("created_at", TimestampType(), False),
        StructField("updated_at", TimestampType(), False),
    ]
    + _cdc_columns()
)

OCR_EXTRACTIONS_SCHEMA = StructType(
    [
        StructField("expense_id", StringType(), False),
        StructField("tenant_id", StringType(), False),
        StructField("user_id", StringType(), True),
        StructField("ocr_total", DecimalType(18, 2), True),
        StructField("ocr_total_confidence", DoubleType(), True),
        StructField("ocr_vendor", StringType(), True),
        StructField("ocr_vendor_confidence", DoubleType(), True),
        StructField("ocr_date", StringType(), True),
        StructField("ocr_date_confidence", DoubleType(), True),
        StructField("ocr_currency", StringType(), True),
        StructField("avg_confidence", DoubleType(), True),
        StructField("textract_raw_s3_key", StringType(), True),
        StructField("extracted_at", TimestampType(), False),
    ]
    + _cdc_columns()
)

EXPENSE_EVENTS_SCHEMA = StructType(
    [
        StructField("event_id", StringType(), False),
        StructField("expense_id", StringType(), False),
        StructField("tenant_id", StringType(), False),
        StructField("event_type", StringType(), False),
        StructField("actor", StringType(), True),
        StructField("details", MapType(StringType(), StringType()), True),
        StructField("workflow_id", StringType(), True),
        StructField("created_at", TimestampType(), False),
    ]
    + _cdc_columns()
)

RECEIPTS_SCHEMA = StructType(
    [
        StructField("receipt_id", StringType(), False),
        StructField("tenant_id", StringType(), False),
        StructField("user_id", StringType(), False),
        StructField("expense_id", StringType(), True),
        StructField("s3_key", StringType(), False),
        StructField("content_type", StringType(), True),
        StructField("size_bytes", LongType(), True),
        StructField("uploaded_at", TimestampType(), False),
    ]
    + _cdc_columns()
)

HITL_TASKS_SCHEMA = StructType(
    [
        StructField("task_id", StringType(), False),
        StructField("tenant_id", StringType(), False),
        StructField("expense_id", StringType(), False),
        StructField("workflow_id", StringType(), True),
        StructField("status", StringType(), False),
        StructField("discrepancy_fields", ArrayType(StringType()), True),
        StructField("decision", StringType(), True),
        StructField("resolved_fields", MapType(StringType(), StringType()), True),
        StructField("resolved_by", StringType(), True),
        StructField("resolved_at", TimestampType(), True),
        StructField("created_at", TimestampType(), False),
    ]
    + _cdc_columns()
)


def _epoch_ms(dt):
    if dt is None:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _to_decimal(v):
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    return Decimal(str(v))


def _iso_date(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    return str(v)[:10]


def seed_collection(coll_name, table_name, schema, to_row):
    client = MongoClient(mongo_uri)
    db = client[mongo_db]
    docs = list(db[coll_name].find({}))
    rows = [to_row(d) for d in docs]
    if not rows:
        print(f"[seed] {coll_name}: 0 docs, skipping table")
        return 0
    df = spark.createDataFrame(rows, schema=schema)
    fq = f"{catalog}.bronze.{table_name}"
    df.write.mode("overwrite").option("mergeSchema", "true").saveAsTable(fq)
    count = spark.table(fq).count()
    print(f"[seed] {coll_name} -> {fq}: {count} rows")
    return count


# COMMAND ----------


def expense_to_row(d):
    return Row(
        expense_id=d["expense_id"],
        tenant_id=d["tenant_id"],
        user_id=d["user_id"],
        receipt_id=d.get("receipt_id"),
        workflow_id=d.get("workflow_id"),
        amount=_to_decimal(d.get("amount")),
        currency=d.get("currency", "USD"),
        date=_iso_date(d.get("date")),
        vendor=d.get("vendor"),
        category=d.get("category"),
        status=d.get("status", "pending"),
        final_amount=_to_decimal(d.get("final_amount")),
        final_vendor=d.get("final_vendor"),
        final_date=_iso_date(d.get("final_date")),
        final_currency=d.get("final_currency"),
        source_per_field={k: str(v) for k, v in (d.get("source_per_field") or {}).items()},
        approved_at=d.get("approved_at"),
        created_at=d.get("created_at"),
        updated_at=d.get("updated_at", d.get("created_at")),
        __op="c",
        __source_ts_ms=_epoch_ms(d.get("updated_at", d.get("created_at"))),
        __deleted=False,
    )


def receipt_to_row(d):
    return Row(
        receipt_id=d["receipt_id"],
        tenant_id=d["tenant_id"],
        user_id=d["user_id"],
        expense_id=d.get("expense_id"),
        s3_key=d["s3_key"],
        content_type=d.get("content_type"),
        size_bytes=d.get("size_bytes"),
        uploaded_at=d.get("uploaded_at", d.get("created_at")),
        __op="c",
        __source_ts_ms=_epoch_ms(d.get("uploaded_at", d.get("created_at"))),
        __deleted=False,
    )


def hitl_to_row(d):
    return Row(
        task_id=d["task_id"],
        tenant_id=d["tenant_id"],
        expense_id=d["expense_id"],
        workflow_id=d.get("workflow_id"),
        status=d.get("status", "pending"),
        discrepancy_fields=d.get("discrepancy_fields") or [],
        decision=d.get("decision"),
        resolved_fields={k: str(v) for k, v in (d.get("resolved_fields") or {}).items()},
        resolved_by=d.get("resolved_by"),
        resolved_at=d.get("resolved_at"),
        created_at=d.get("created_at"),
        __op="c",
        __source_ts_ms=_epoch_ms(d.get("resolved_at", d.get("created_at"))),
        __deleted=False,
    )


def ocr_to_row(d):
    return Row(
        expense_id=d["expense_id"],
        tenant_id=d["tenant_id"],
        user_id=d.get("user_id"),
        ocr_total=_to_decimal(d.get("ocr_total")),
        ocr_total_confidence=d.get("ocr_total_confidence"),
        ocr_vendor=d.get("ocr_vendor"),
        ocr_vendor_confidence=d.get("ocr_vendor_confidence"),
        ocr_date=_iso_date(d.get("ocr_date")),
        ocr_date_confidence=d.get("ocr_date_confidence"),
        ocr_currency=d.get("ocr_currency"),
        avg_confidence=d.get("avg_confidence"),
        textract_raw_s3_key=d.get("textract_raw_s3_key"),
        extracted_at=d.get("extracted_at", d.get("created_at")),
        __op="c",
        __source_ts_ms=_epoch_ms(d.get("extracted_at", d.get("created_at"))),
        __deleted=False,
    )


def event_to_row(d):
    return Row(
        event_id=d["event_id"],
        expense_id=d["expense_id"],
        tenant_id=d["tenant_id"],
        event_type=d["event_type"],
        actor=d.get("actor"),
        details={k: str(v) for k, v in (d.get("details") or {}).items()},
        workflow_id=d.get("workflow_id"),
        created_at=d["created_at"],
        __op="c",
        __source_ts_ms=_epoch_ms(d["created_at"]),
        __deleted=False,
    )


# COMMAND ----------
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {catalog}.bronze")

total = 0
total += seed_collection("expenses", "mongodb_cdc_expenses", EXPENSES_SCHEMA, expense_to_row)
total += seed_collection("receipts", "mongodb_cdc_receipts", RECEIPTS_SCHEMA, receipt_to_row)
total += seed_collection("hitl_tasks", "mongodb_cdc_hitl_tasks", HITL_TASKS_SCHEMA, hitl_to_row)
total += seed_collection(
    "ocr_extractions", "mongodb_cdc_ocr_extractions", OCR_EXTRACTIONS_SCHEMA, ocr_to_row
)
total += seed_collection(
    "expense_events", "mongodb_cdc_expense_events", EXPENSE_EVENTS_SCHEMA, event_to_row
)

print(f"[seed] total rows across 5 tables: {total}")
