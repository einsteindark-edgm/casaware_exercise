"""Textract activities. Real mode uses boto3 AnalyzeExpense / AnalyzeDocument.

When settings.use_fake_textract is True (FAKE_TEXTRACT env or FAKE_PROVIDERS
fallback), returns deterministic synthetic results so the workflow can run
against LocalStack S3 without an AWS Textract endpoint.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from nexus_orchestration.activities._fakes import fake_textract_extract
from nexus_orchestration.config import settings
from nexus_orchestration.observability.logging import get_logger

log = get_logger(__name__)


def _user_reported_from_input(inp: dict[str, Any]) -> dict[str, Any]:
    # Allows the fake branch to produce a deterministic delta relative to what
    # the user actually reported for this expense.
    return inp.get("user_reported_data") or {}


def _resolved_bucket(inp: dict[str, Any]) -> str:
    return inp.get("s3_bucket") or settings.s3_receipts_bucket


@activity.defn(name="textract_analyze_expense")
async def textract_analyze_expense(inp: dict[str, Any]) -> dict[str, Any]:
    log.info("textract.start", s3_key=inp["s3_key"], fake=settings.use_fake_textract)

    if settings.use_fake_textract:
        return fake_textract_extract(inp["s3_key"], _user_reported_from_input(inp))

    import boto3  # type: ignore[import-not-found]

    client = boto3.client("textract", region_name=settings.aws_region)
    try:
        response = client.analyze_expense(
            Document={"S3Object": {"Bucket": _resolved_bucket(inp), "Name": inp["s3_key"]}}
        )
    except client.exceptions.UnsupportedDocumentException as exc:
        raise ApplicationError(
            "UnsupportedMimeType", type="UnsupportedMimeTypeError"
        ) from exc

    # Persist the raw JSON for lineage.
    s3 = boto3.client("s3", region_name=settings.aws_region)
    output_key = (
        f"tenant={inp['tenant_id']}/expense={inp['expense_id']}/{uuid.uuid4()}.json"
    )
    s3.put_object(
        Bucket=settings.s3_textract_output_bucket,
        Key=output_key,
        Body=json.dumps(response).encode("utf-8"),
        ContentType="application/json",
    )

    summary_fields = _normalize_summary_fields(response)
    extra = _extract_extra(response)
    return {
        "raw_output_s3_key": output_key,
        "fields": summary_fields,
        "avg_confidence": _compute_avg_confidence(summary_fields),
        "fields_summary": _build_summary(summary_fields, extra),
        "ocr_extra": extra,
    }


@activity.defn(name="textract_analyze_document_queries")
async def textract_analyze_document_queries(inp: dict[str, Any]) -> dict[str, Any]:
    if settings.use_fake_textract:
        # Fall back to the same fake — if we got here, something wasn't great
        # about the first attempt; bump confidence slightly so the caller can
        # tell the queries branch ran.
        base = fake_textract_extract(inp["s3_key"], inp.get("user_reported_data") or {})
        for f in base["fields"].values():
            if isinstance(f, dict) and "confidence" in f:
                f["confidence"] = min(99.9, float(f["confidence"]) + 2.0)
        base["avg_confidence"] = round(
            sum(f["confidence"] for f in base["fields"].values()) / len(base["fields"]), 2
        )
        return base

    import boto3  # type: ignore[import-not-found]

    client = boto3.client("textract", region_name=settings.aws_region)
    response = client.analyze_document(
        Document={"S3Object": {"Bucket": _resolved_bucket(inp), "Name": inp["s3_key"]}},
        FeatureTypes=["QUERIES"],
        QueriesConfig={"Queries": inp["queries"]},
    )
    return _response_from_queries(response)


def _normalize_summary_fields(response: dict[str, Any]) -> dict[str, Any]:
    """Maps the 3 'authoritative' Textract fields used by silver/gold joins.

    Anything outside this mapping flows into `ocr_extra` instead — see
    `_extract_extra`. Keep this list in lockstep with the bronze CDC schema
    (`nexus-medallion/src/bronze/cdc_ocr_extractions.py`) and the
    silver→gold ocr enrichment in `gold/expense_audit.py`.
    """
    out: dict[str, Any] = {}
    mapping = {
        "TOTAL": "ocr_total",
        "VENDOR_NAME": "ocr_vendor",
        "INVOICE_RECEIPT_DATE": "ocr_date",
    }
    for doc in response.get("ExpenseDocuments", []):
        for field in doc.get("SummaryFields", []):
            key = (field.get("Type") or {}).get("Text", "OTHER")
            normalized = mapping.get(key)
            if not normalized:
                continue
            value_detection = field.get("ValueDetection") or {}
            out[normalized] = {
                "value": value_detection.get("Text"),
                "confidence": value_detection.get("Confidence", 0.0),
            }
    return out


# Textract SummaryField types we want as named columns (lowercase) inside
# `ocr_extra.summary_fields`. Anything Textract returns that's not in this
# list still makes it through tagged as its raw Type — we'd rather over-
# capture than drop something that could be useful for RAG.
_KNOWN_SUMMARY_TYPES = {
    "SUBTOTAL": "subtotal",
    "TAX": "tax",
    "AMOUNT_PAID": "amount_paid",
    "AMOUNT_DUE": "amount_due",
    "INVOICE_RECEIPT_ID": "invoice_id",
    "PO_NUMBER": "po_number",
    "RECEIVER_NAME": "receiver_name",
    "RECEIVER_ADDRESS": "receiver_address",
    "VENDOR_ADDRESS": "vendor_address",
    "VENDOR_PHONE": "vendor_phone",
    "VENDOR_URL": "vendor_url",
    "VENDOR_GST_NUMBER": "vendor_tax_id",
    "PAYMENT_TERMS": "payment_terms",
    "DUE_DATE": "due_date",
    "DELIVERY_DATE": "delivery_date",
    "ORDER_DATE": "order_date",
    "ACCOUNT_NUMBER": "account_number",
    "TRACKING_NUMBER": "tracking_number",
    "STREET": "street",
    "CITY": "city",
    "STATE": "state",
    "ZIP_CODE": "zip_code",
    "COUNTRY": "country",
    "ADDRESS_BLOCK": "address_block",
}

# These three are owned by `_normalize_summary_fields`; don't duplicate them
# under `ocr_extra` or the chunk_text would say the total twice.
_AUTHORITATIVE_TYPES = {"TOTAL", "VENDOR_NAME", "INVOICE_RECEIPT_DATE"}


def _extract_extra(response: dict[str, Any]) -> dict[str, Any]:
    """Capture every Textract field that isn't one of the 3 authoritative ones.

    Returns:
        {
          "summary_fields": [{field, value, confidence}, ...],
          "line_items":    [{item, price, quantity, confidence_avg}, ...],
        }

    Both lists come from Textract `analyze_expense`. Confidence is preserved
    so the UI can color-code low-confidence values, and so the embedding
    can later weight (or skip) noisy fields.
    """
    summary_extra: list[dict[str, Any]] = []
    line_items: list[dict[str, Any]] = []

    for doc in response.get("ExpenseDocuments", []):
        for field in doc.get("SummaryFields", []):
            raw_type = (field.get("Type") or {}).get("Text", "OTHER")
            if raw_type in _AUTHORITATIVE_TYPES:
                continue
            value_detection = field.get("ValueDetection") or {}
            label = field.get("LabelDetection") or {}
            summary_extra.append(
                {
                    "field": _KNOWN_SUMMARY_TYPES.get(raw_type, raw_type.lower()),
                    "value": value_detection.get("Text"),
                    "confidence": value_detection.get("Confidence", 0.0),
                    "label_text": label.get("Text"),
                }
            )

        for group in doc.get("LineItemGroups", []):
            for li in group.get("LineItems", []):
                cells: dict[str, Any] = {}
                confs: list[float] = []
                for f in li.get("LineItemExpenseFields", []):
                    raw_type = (f.get("Type") or {}).get("Text", "OTHER")
                    value_detection = f.get("ValueDetection") or {}
                    txt = value_detection.get("Text")
                    conf = float(value_detection.get("Confidence") or 0.0)
                    if conf:
                        confs.append(conf)
                    if raw_type == "ITEM":
                        cells["item"] = txt
                    elif raw_type == "PRICE":
                        cells["price"] = txt
                    elif raw_type == "QUANTITY":
                        cells["quantity"] = txt
                    elif raw_type == "UNIT_PRICE":
                        cells["unit_price"] = txt
                    elif raw_type == "PRODUCT_CODE":
                        cells["product_code"] = txt
                    elif raw_type == "EXPENSE_ROW":
                        cells.setdefault("row_text", txt)
                    else:
                        cells[raw_type.lower()] = txt
                if cells:
                    cells["confidence_avg"] = (
                        round(sum(confs) / len(confs), 2) if confs else 0.0
                    )
                    line_items.append(cells)

    return {"summary_fields": summary_extra, "line_items": line_items}


def _compute_avg_confidence(fields: dict[str, Any]) -> float:
    if not fields:
        return 0.0
    confidences = [
        float(v.get("confidence") or 0.0) for v in fields.values() if isinstance(v, dict)
    ]
    return round(sum(confidences) / max(len(confidences), 1), 2)


def _build_summary(
    fields: dict[str, Any], extra: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Flat list rendered in the timeline event (ocr_completed.details).

    Authoritative fields first (total / vendor / date), then everything in
    `extra.summary_fields`. Line items are NOT in this list — the UI shows
    them in a separate section.
    """
    out: list[dict[str, Any]] = [
        {"field": k, "value": v.get("value"), "confidence": v.get("confidence")}
        for k, v in fields.items()
    ]
    if extra:
        for f in extra.get("summary_fields", []):
            out.append(
                {
                    "field": f.get("field"),
                    "value": f.get("value"),
                    "confidence": f.get("confidence"),
                }
            )
    return out


def _response_from_queries(response: dict[str, Any]) -> dict[str, Any]:
    # Best-effort extraction of QUERY_RESULT blocks.
    fields: dict[str, Any] = {}
    blocks = response.get("Blocks", [])
    aliases = {}
    for block in blocks:
        if block.get("BlockType") == "QUERY":
            qid = block.get("Id")
            alias = (block.get("Query") or {}).get("Alias")
            aliases[qid] = alias
    for block in blocks:
        if block.get("BlockType") != "QUERY_RESULT":
            continue
        # Correlate back to the parent QUERY via Relationships.
        text = block.get("Text")
        conf = block.get("Confidence", 0.0)
        for alias in aliases.values():
            if not alias:
                continue
            mapping = {"TOTAL": "ocr_total", "VENDOR": "ocr_vendor", "DATE": "ocr_date"}
            key = mapping.get(alias)
            if key and key not in fields:
                fields[key] = {"value": text, "confidence": conf}
                break
    extra = {"summary_fields": [], "line_items": []}
    return {
        "raw_output_s3_key": None,
        "fields": fields,
        "avg_confidence": _compute_avg_confidence(fields),
        "fields_summary": _build_summary(fields, extra),
        "ocr_extra": extra,
    }
