"""OCRExtractionWorkflow — Textract call with an optional queries-fallback.

Runs in `nexus-ocr-tq`. Emits `ocr_started` / `ocr_completed` to the expense
timeline and publishes `workflow.ocr_progress` to the user's Redis channel.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn(name="OCRExtractionWorkflow")
class OCRExtractionWorkflow:
    @workflow.run
    async def run(self, inp: dict[str, Any]) -> dict[str, Any]:
        expense_id = inp["expense_id"]
        tenant_id = inp["tenant_id"]
        user_id = inp["user_id"]

        # 1) Timeline entry.
        await workflow.execute_activity(
            "emit_expense_event",
            {
                "expense_id": expense_id,
                "tenant_id": tenant_id,
                "event_type": "ocr_started",
                "actor": {"type": "system", "id": "worker:ocr"},
                "details": {"s3_key": inp["receipt_s3_key"]},
            },
            start_to_close_timeout=timedelta(seconds=5),
        )

        # 2) Progress event for the SSE feed.
        await workflow.execute_activity(
            "publish_event",
            {
                "event_type": "workflow.ocr_progress",
                "tenant_id": tenant_id,
                "user_id": user_id,
                "expense_id": expense_id,
                "payload": {"step": "textract_call", "progress_pct": 10},
            },
            start_to_close_timeout=timedelta(seconds=5),
        )

        # 3) Call Textract.
        result = await workflow.execute_activity(
            "textract_analyze_expense",
            {
                "s3_bucket": inp["s3_bucket"],
                "s3_key": inp["receipt_s3_key"],
                "tenant_id": tenant_id,
                "expense_id": expense_id,
                "user_reported_data": inp.get("user_reported_data") or {},
            },
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(
                maximum_attempts=3,
                non_retryable_error_types=["UnsupportedMimeTypeError"],
            ),
        )

        # 4) Fallback to Queries if confidence is low.
        if result.get("avg_confidence", 0) < 80:
            await workflow.execute_activity(
                "publish_event",
                {
                    "event_type": "workflow.ocr_progress",
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "expense_id": expense_id,
                    "payload": {"step": "queries_fallback", "progress_pct": 50},
                },
                start_to_close_timeout=timedelta(seconds=5),
            )
            queries_result = await workflow.execute_activity(
                "textract_analyze_document_queries",
                {
                    "s3_bucket": inp["s3_bucket"],
                    "s3_key": inp["receipt_s3_key"],
                    "tenant_id": tenant_id,
                    "expense_id": expense_id,
                    "user_reported_data": inp.get("user_reported_data") or {},
                    "queries": [
                        {"Text": "What is the total amount?", "Alias": "TOTAL"},
                        {"Text": "What is the date?", "Alias": "DATE"},
                        {"Text": "What is the vendor name?", "Alias": "VENDOR"},
                    ],
                },
                start_to_close_timeout=timedelta(seconds=60),
            )
            result = _merge_extractions(result, queries_result)

        # 5) Persist OCR result to Mongo.
        await workflow.execute_activity(
            "upsert_ocr_extraction",
            {
                "expense_id": expense_id,
                "tenant_id": tenant_id,
                "user_id": user_id,
                **result["fields"],
                "avg_confidence": result["avg_confidence"],
                "textract_raw_s3_key": result.get("raw_output_s3_key"),
                "ocr_extra": result.get("ocr_extra")
                or {"summary_fields": [], "line_items": []},
            },
            start_to_close_timeout=timedelta(seconds=10),
        )

        # 6) Timeline close-out.
        await workflow.execute_activity(
            "emit_expense_event",
            {
                "expense_id": expense_id,
                "tenant_id": tenant_id,
                "event_type": "ocr_completed",
                "actor": {"type": "system", "id": "worker:ocr"},
                "details": {
                    "avg_confidence": result["avg_confidence"],
                    "fields_summary": result.get("fields_summary", []),
                    "line_items": (result.get("ocr_extra") or {}).get("line_items", []),
                },
            },
            start_to_close_timeout=timedelta(seconds=5),
        )

        await workflow.execute_activity(
            "publish_event",
            {
                "event_type": "workflow.ocr_progress",
                "tenant_id": tenant_id,
                "user_id": user_id,
                "expense_id": expense_id,
                "payload": {"step": "completed", "progress_pct": 100},
            },
            start_to_close_timeout=timedelta(seconds=5),
        )

        return result


def _merge_extractions(primary: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    """Prefer field-by-field whichever source has higher confidence."""
    merged_fields: dict[str, Any] = {}
    all_keys = set(primary.get("fields", {})) | set(fallback.get("fields", {}))
    for key in all_keys:
        a = primary.get("fields", {}).get(key) or {}
        b = fallback.get("fields", {}).get(key) or {}
        a_conf = float(a.get("confidence") or 0.0)
        b_conf = float(b.get("confidence") or 0.0)
        merged_fields[key] = a if a_conf >= b_conf else b
    confidences = [
        float(v.get("confidence") or 0.0)
        for v in merged_fields.values()
        if isinstance(v, dict)
    ]
    avg = round(sum(confidences) / max(len(confidences), 1), 2)
    # Queries fallback never produces extras (it only re-asks the 3 core
    # fields), so `ocr_extra` flows through from the primary AnalyzeExpense
    # response. If the primary returned nothing, fall back to whatever the
    # queries branch provided (likely empty lists).
    extra = primary.get("ocr_extra") or fallback.get("ocr_extra") or {
        "summary_fields": [],
        "line_items": [],
    }
    summary = [
        {"field": k, "value": v.get("value"), "confidence": v.get("confidence")}
        for k, v in merged_fields.items()
    ]
    for f in extra.get("summary_fields", []):
        summary.append(
            {
                "field": f.get("field"),
                "value": f.get("value"),
                "confidence": f.get("confidence"),
            }
        )
    return {
        "raw_output_s3_key": primary.get("raw_output_s3_key")
        or fallback.get("raw_output_s3_key"),
        "fields": merged_fields,
        "avg_confidence": avg,
        "fields_summary": summary,
        "ocr_extra": extra,
    }
