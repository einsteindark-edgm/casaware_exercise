"""Textract activities. Real mode uses boto3 AnalyzeExpense / AnalyzeDocument.

When settings.fake_providers is True, returns deterministic synthetic results
so the workflow can run against LocalStack S3 without an AWS Textract endpoint.
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
    log.info("textract.start", s3_key=inp["s3_key"], fake=settings.fake_providers)

    if settings.fake_providers:
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
    return {
        "raw_output_s3_key": output_key,
        "fields": summary_fields,
        "avg_confidence": _compute_avg_confidence(summary_fields),
        "fields_summary": _build_summary(summary_fields),
    }


@activity.defn(name="textract_analyze_document_queries")
async def textract_analyze_document_queries(inp: dict[str, Any]) -> dict[str, Any]:
    if settings.fake_providers:
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


def _compute_avg_confidence(fields: dict[str, Any]) -> float:
    if not fields:
        return 0.0
    confidences = [
        float(v.get("confidence") or 0.0) for v in fields.values() if isinstance(v, dict)
    ]
    return round(sum(confidences) / max(len(confidences), 1), 2)


def _build_summary(fields: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"field": k, "value": v.get("value"), "confidence": v.get("confidence")}
        for k, v in fields.items()
    ]


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
    return {
        "raw_output_s3_key": None,
        "fields": fields,
        "avg_confidence": _compute_avg_confidence(fields),
        "fields_summary": _build_summary(fields),
    }
