"""Schemas compartidos por seed y silver.

Reflejan las colecciones de Mongo que el backend + worker ya escriben.
Referencia: 00-contratos-compartidos.md §2.6 y el código del backend
(backend/src/nexus_backend/domain/expense/models.py).
"""
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


def _cdc_columns() -> list[StructField]:
    """Columnas de envelope CDC que añade seed/debezium en bronze."""
    return [
        StructField("__op", StringType(), nullable=False),  # 'c' | 'u' | 'd'
        StructField("__source_ts_ms", LongType(), nullable=False),
        StructField("__deleted", BooleanType(), nullable=False),
    ]


EXPENSES_SCHEMA = StructType(
    [
        StructField("expense_id", StringType(), nullable=False),
        StructField("tenant_id", StringType(), nullable=False),
        StructField("user_id", StringType(), nullable=False),
        StructField("receipt_id", StringType(), nullable=True),
        StructField("workflow_id", StringType(), nullable=True),
        StructField("amount", DecimalType(18, 2), nullable=False),
        StructField("currency", StringType(), nullable=False),
        StructField("date", StringType(), nullable=True),  # ISO date
        StructField("vendor", StringType(), nullable=True),
        StructField("category", StringType(), nullable=True),
        StructField("status", StringType(), nullable=False),
        # Campos que el worker embebe tras el approval (doc 03 §4):
        StructField("final_amount", DecimalType(18, 2), nullable=True),
        StructField("final_vendor", StringType(), nullable=True),
        StructField("final_date", StringType(), nullable=True),
        StructField("final_currency", StringType(), nullable=True),
        StructField("source_per_field", MapType(StringType(), StringType()), nullable=True),
        StructField("approved_at", TimestampType(), nullable=True),
        StructField("created_at", TimestampType(), nullable=False),
        StructField("updated_at", TimestampType(), nullable=False),
    ]
    + _cdc_columns()
)

OCR_EXTRACTIONS_SCHEMA = StructType(
    [
        StructField("expense_id", StringType(), nullable=False),
        StructField("tenant_id", StringType(), nullable=False),
        StructField("user_id", StringType(), nullable=True),
        StructField("ocr_total", DecimalType(18, 2), nullable=True),
        StructField("ocr_total_confidence", DoubleType(), nullable=True),
        StructField("ocr_vendor", StringType(), nullable=True),
        StructField("ocr_vendor_confidence", DoubleType(), nullable=True),
        StructField("ocr_date", StringType(), nullable=True),
        StructField("ocr_date_confidence", DoubleType(), nullable=True),
        StructField("ocr_currency", StringType(), nullable=True),
        StructField("avg_confidence", DoubleType(), nullable=True),
        StructField("textract_raw_s3_key", StringType(), nullable=True),
        StructField("extracted_at", TimestampType(), nullable=False),
    ]
    + _cdc_columns()
)

EXPENSE_EVENTS_SCHEMA = StructType(
    [
        StructField("event_id", StringType(), nullable=False),
        StructField("expense_id", StringType(), nullable=False),
        StructField("tenant_id", StringType(), nullable=False),
        StructField("event_type", StringType(), nullable=False),
        StructField("actor", StringType(), nullable=True),
        StructField("details", MapType(StringType(), StringType()), nullable=True),
        StructField("workflow_id", StringType(), nullable=True),
        StructField("created_at", TimestampType(), nullable=False),
    ]
    + _cdc_columns()
)

RECEIPTS_SCHEMA = StructType(
    [
        StructField("receipt_id", StringType(), nullable=False),
        StructField("tenant_id", StringType(), nullable=False),
        StructField("user_id", StringType(), nullable=False),
        StructField("expense_id", StringType(), nullable=True),
        StructField("s3_key", StringType(), nullable=False),
        StructField("content_type", StringType(), nullable=True),
        StructField("size_bytes", LongType(), nullable=True),
        StructField("uploaded_at", TimestampType(), nullable=False),
    ]
    + _cdc_columns()
)

HITL_TASKS_SCHEMA = StructType(
    [
        StructField("hitl_id", StringType(), nullable=False),
        StructField("tenant_id", StringType(), nullable=False),
        StructField("expense_id", StringType(), nullable=False),
        StructField("workflow_id", StringType(), nullable=True),
        StructField("status", StringType(), nullable=False),
        StructField("discrepancy_fields", ArrayType(StringType()), nullable=True),
        StructField("decision", StringType(), nullable=True),
        StructField("resolved_fields", MapType(StringType(), StringType()), nullable=True),
        StructField("resolved_by", StringType(), nullable=True),
        StructField("resolved_at", TimestampType(), nullable=True),
        StructField("created_at", TimestampType(), nullable=False),
    ]
    + _cdc_columns()
)
