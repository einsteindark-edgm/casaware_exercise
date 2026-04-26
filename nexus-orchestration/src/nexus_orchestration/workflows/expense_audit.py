"""ExpenseAuditWorkflow — parent workflow for the whole audit lifecycle.

Differences vs doc 03 §4:
  - Does NOT publish `workflow.started` (the backend already publishes it in
    POST /expenses).
  - Does NOT publish `workflow.hitl_resolved` (the backend publishes it in
    POST /hitl/{id}/resolve).

Runs in `nexus-orchestrator-tq`. Spawns child workflows on `nexus-ocr-tq` and
`nexus-databricks-tq`.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

# Textract returns TOTAL as raw label text (e.g. "24,395.00 COP"). We must
# normalise to a float before it lands in `final_data` — the Mongo write,
# the silver/gold pipelines, and ExpenseRead all expect a number. Same
# heuristic as activities/comparison.py, duplicated here because workflow
# code must stay deterministic and self-contained.
_OCR_AMOUNT_STRIP = re.compile(r"[^\d,.\-]")


def _parse_ocr_amount(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = _OCR_AMOUNT_STRIP.sub("", str(value)).strip()
    if not cleaned:
        return None
    last_dot = cleaned.rfind(".")
    last_comma = cleaned.rfind(",")
    if last_comma > last_dot:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


# Hermano del bug del amount: Textract devuelve fechas en el formato del
# recibo (`25/04/2026` para LATAM/EU, `04/25/2026` para US, etc), pero
# `expenses.final_date` es un campo `date` en Pydantic v2 que sólo acepta
# ISO. Sin esta normalización, el HITL accept_ocr escribe la cadena cruda
# y el `GET /api/v1/expenses` retorna 500 al validar la lista (incidente
# 2026-04-26 sobre exp_01KQ3K99MGEMH309M7E60DW16B con `final_date="25/04/2026"`).
# Probamos formatos en orden de probabilidad regional; LATAM/EU primero
# porque es nuestro tenant principal. Si ninguno parsea, retornamos None
# en lugar de la cadena cruda — Mongo NULL es preferible a un 500 en read.
_OCR_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d")


def _parse_ocr_date(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        return None
    s = value.strip()
    for fmt in _OCR_DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None

with workflow.unsafe.imports_passed_through():
    from nexus_orchestration.workflows.audit_validation import AuditValidationWorkflow
    from nexus_orchestration.workflows.ocr_extraction import OCRExtractionWorkflow


@workflow.defn(name="ExpenseAuditWorkflow")
class ExpenseAuditWorkflow:
    def __init__(self) -> None:
        self._state: str = "running"
        self._current_step: str = "initializing"
        self._hitl_response: dict[str, Any] | None = None
        self._hitl_task_id: str | None = None
        self._cancelled: bool = False
        self._fields_in_conflict: list[dict[str, Any]] = []

    @workflow.run
    async def run(self, inp: dict[str, Any]) -> dict[str, Any]:
        expense_id = inp["expense_id"]
        tenant_id = inp["tenant_id"]
        user_id = inp["user_id"]

        async def _emit_step(step: str) -> None:
            # Best-effort SSE breadcrumb; failures must not break the audit.
            await workflow.execute_activity(
                "publish_event",
                {
                    "event_type": "workflow.step_changed",
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "expense_id": expense_id,
                    "payload": {"step": step},
                },
                start_to_close_timeout=timedelta(seconds=5),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )

        await _emit_step("processing")

        # Mark expense as processing (backend left it pending).
        await workflow.execute_activity(
            "update_expense_status",
            {"expense_id": expense_id, "tenant_id": tenant_id, "status": "processing"},
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

        # 1) Child: OCR extraction. The OCR activity defaults s3_bucket to
        # settings.s3_receipts_bucket when not provided, so we don't have to
        # read `settings` from the workflow (determinism).
        self._current_step = "ocr_extraction"
        await _emit_step("ocr_extraction")
        ocr_result = await workflow.execute_child_workflow(
            OCRExtractionWorkflow.run,
            {
                "expense_id": expense_id,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "receipt_s3_key": inp["receipt_s3_key"],
                "s3_bucket": inp.get("s3_bucket"),
                "user_reported_data": inp.get("user_reported_data") or {},
            },
            id=f"ocr-{expense_id}",
            task_queue="nexus-ocr-tq",
            retry_policy=RetryPolicy(
                maximum_attempts=3,
                non_retryable_error_types=["UnsupportedMimeTypeError"],
            ),
        )

        # 2) Child: audit validation.
        self._current_step = "audit_validation"
        await _emit_step("audit_validation")
        audit_result = await workflow.execute_child_workflow(
            AuditValidationWorkflow.run,
            {
                "expense_input": {
                    "expense_id": expense_id,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "user_reported_data": inp.get("user_reported_data") or {},
                },
                "ocr_result": ocr_result,
            },
            id=f"audit-{expense_id}",
            task_queue="nexus-databricks-tq",
        )

        if audit_result["has_discrepancies"]:
            self._state = "waiting_hitl"
            self._current_step = "waiting_human"
            self._hitl_task_id = audit_result["hitl_task_id"]
            self._fields_in_conflict = audit_result["fields_in_conflict"]

            # Update expense.status so the frontend can filter.
            await workflow.execute_activity(
                "update_expense_status",
                {
                    "expense_id": expense_id,
                    "tenant_id": tenant_id,
                    "status": "hitl_required",
                },
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )

            # Timeline + SSE notification.
            await workflow.execute_activity(
                "emit_expense_event",
                {
                    "expense_id": expense_id,
                    "tenant_id": tenant_id,
                    "event_type": "hitl_required",
                    "actor": {"type": "system", "id": "worker:orchestrator"},
                    "details": {
                        "hitl_task_id": self._hitl_task_id,
                        "fields_in_conflict": self._fields_in_conflict,
                    },
                },
                start_to_close_timeout=timedelta(seconds=5),
            )
            await workflow.execute_activity(
                "publish_event",
                {
                    "event_type": "workflow.hitl_required",
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "expense_id": expense_id,
                    "payload": {
                        "hitl_task_id": self._hitl_task_id,
                        "fields_in_conflict": self._fields_in_conflict,
                    },
                },
                start_to_close_timeout=timedelta(seconds=5),
            )

            # Wait for the signal or cancellation — 7 day timeout.
            try:
                await workflow.wait_condition(
                    lambda: self._hitl_response is not None or self._cancelled,
                    timeout=timedelta(days=7),
                )
            except TimeoutError:
                await self._fail(
                    expense_id, tenant_id, user_id, "HITL_TIMEOUT", "timeout after 7 days"
                )
                return {"status": "failed", "reason": "HITL_TIMEOUT"}

            if self._cancelled:
                await self._fail(
                    expense_id, tenant_id, user_id, "CANCELLED", "cancelled by user"
                )
                return {"status": "cancelled"}

            final_data = _apply_hitl_resolution(audit_result, self._hitl_response or {})
            source_per_field = _source_per_field(audit_result, self._hitl_response or {})
        else:
            final_data = audit_result["user_reported"] or _from_ocr(
                audit_result["extracted_data"]
            )
            source_per_field = {field: "ocr_high_confidence" for field in final_data}

        # 3) Finalise in Mongo.
        self._current_step = "finalizing_in_mongo"
        await _emit_step("finalizing")
        await workflow.execute_activity(
            "update_expense_to_approved",
            {
                "expense_id": expense_id,
                "tenant_id": tenant_id,
                "final_data": final_data,
                "source_per_field": source_per_field,
            },
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )

        # 4) Timeline entry.
        await workflow.execute_activity(
            "emit_expense_event",
            {
                "expense_id": expense_id,
                "tenant_id": tenant_id,
                "event_type": "approved",
                "actor": {
                    "type": "system",
                    "id": "worker:nexus-orchestrator-tq",
                },
                "details": {
                    "final_data": final_data,
                    "source_per_field": source_per_field,
                },
            },
            start_to_close_timeout=timedelta(seconds=5),
        )

        # 5) Vector sync — best-effort observability, NOT a gate on approval.
        #
        # The activity polls gold.expense_chunks (~10 min DLT cadence) and
        # MERGEs the embedding into gold.expense_embeddings. Two prior
        # designs were both wrong:
        #   - Pre-2026-04-25: swallow failures silently → workflow closed
        #     `completed` while embedding crashed (expense
        #     01KQ37YXE34Q4JWEQ17CQRX5WQ, RuntimeError: no running event
        #     loop). Chat agent had no embeddings for that receipt.
        #   - Post-2026-04-25: call self._fail on any vector_sync failure
        #     → that flips the expense status to `rejected`. But
        #     gold.expense_audit filters `status = 'approved'`, so the
        #     once-approved row was *removed* from gold whenever the gold
        #     MV hadn't materialized within the 18-min polling window —
        #     particularly common after HITL approves, where silver/gold
        #     are still catching up to the fresh status=approved write.
        #
        # The HITL-resolved expense is approved by a human; that's the
        # canonical business terminal state and must reach gold. A missing
        # embedding is independently recoverable (the MERGE is idempotent,
        # so an out-of-band retry can fill it in). We emit a timeline +
        # SSE event so the failure is observable, but we do NOT touch the
        # expense status.
        self._current_step = "vectorizing"
        await _emit_step("vectorizing")
        vec_result: dict[str, Any] = await workflow.execute_activity(
            "trigger_vector_sync",
            {"expense_id": expense_id, "tenant_id": tenant_id},
            start_to_close_timeout=timedelta(minutes=22),
            # Activity heartbeats every 60s during the gold-MV polling loop.
            # 3 minutes gives generous slack for TCP/SQL latency without
            # letting a truly stuck activity hold up the workflow for 22m.
            heartbeat_timeout=timedelta(minutes=3),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
        vec_status = (vec_result or {}).get("status")
        if vec_status in ("failed", "timed_out_not_in_gold"):
            err_type = (vec_result or {}).get("error", vec_status)
            err_msg = (
                (vec_result or {}).get("message")
                or (
                    "gold.expense_chunks did not materialize within polling window"
                    if vec_status == "timed_out_not_in_gold"
                    else ""
                )
            )
            await workflow.execute_activity(
                "emit_expense_event",
                {
                    "expense_id": expense_id,
                    "tenant_id": tenant_id,
                    "event_type": "vector_sync_failed",
                    "actor": {"type": "system", "id": "worker:orchestrator"},
                    "details": {
                        "status": vec_status,
                        "error": err_type,
                        "message": err_msg,
                    },
                },
                start_to_close_timeout=timedelta(seconds=5),
            )
            await workflow.execute_activity(
                "publish_event",
                {
                    "event_type": "workflow.vector_sync_failed",
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "expense_id": expense_id,
                    "payload": {
                        "status": vec_status,
                        "error": err_type,
                        "message": err_msg,
                    },
                },
                start_to_close_timeout=timedelta(seconds=5),
            )

        # 6) Redis completion event.
        self._state = "completed"
        self._current_step = "completed"
        await workflow.execute_activity(
            "publish_event",
            {
                "event_type": "workflow.completed",
                "tenant_id": tenant_id,
                "user_id": user_id,
                "expense_id": expense_id,
                "payload": {"final_state": "approved"},
            },
            start_to_close_timeout=timedelta(seconds=5),
        )

        return {"status": "completed", "final_data": final_data}

    async def _fail(
        self,
        expense_id: str,
        tenant_id: str,
        user_id: str,
        error_code: str,
        message: str,
    ) -> None:
        self._state = "failed"
        self._current_step = f"failed:{error_code}"
        await workflow.execute_activity(
            "update_expense_to_rejected",
            {
                "expense_id": expense_id,
                "tenant_id": tenant_id,
                "reason": error_code,
            },
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        await workflow.execute_activity(
            "emit_expense_event",
            {
                "expense_id": expense_id,
                "tenant_id": tenant_id,
                "event_type": "failed",
                "actor": {"type": "system", "id": "worker:orchestrator"},
                "details": {"reason": error_code, "message": message},
            },
            start_to_close_timeout=timedelta(seconds=5),
        )
        await workflow.execute_activity(
            "publish_event",
            {
                "event_type": "workflow.failed",
                "tenant_id": tenant_id,
                "user_id": user_id,
                "expense_id": expense_id,
                "payload": {"error_code": error_code, "message": message},
            },
            start_to_close_timeout=timedelta(seconds=5),
        )

    # ── Signals ────────────────────────────────────────────────────────────
    @workflow.signal(name="hitl_response")
    async def hitl_response(self, response: dict[str, Any]) -> None:
        self._hitl_response = response

    @workflow.signal(name="cancel_audit")
    async def cancel_audit(self, payload: dict[str, Any]) -> None:
        self._cancelled = True

    # ── Queries ────────────────────────────────────────────────────────────
    @workflow.query(name="get_status")
    def get_status(self) -> dict[str, Any]:
        return {"state": self._state, "current_step": self._current_step}

    @workflow.query(name="get_hitl_data")
    def get_hitl_data(self) -> dict[str, Any] | None:
        if self._hitl_task_id is None:
            return None
        return {
            "task_id": self._hitl_task_id,
            "fields_in_conflict": self._fields_in_conflict,
        }

    @workflow.query(name="get_history")
    def get_history(self) -> list[dict[str, Any]]:
        # Minimal history — detailed audit trail lives in expense_events (Mongo).
        return [{"step": self._current_step, "state": self._state}]


def _apply_hitl_resolution(
    audit_result: dict[str, Any], hitl_response: dict[str, Any]
) -> dict[str, Any]:
    """Combine user-reported / OCR / custom values based on the HITL decision."""
    decision = hitl_response.get("decision", "accept_ocr")
    resolved = hitl_response.get("resolved_fields") or {}
    user_reported = audit_result.get("user_reported") or {}
    ocr_fields = _from_ocr(audit_result.get("extracted_data") or {})

    base = {
        "amount": user_reported.get("amount"),
        "vendor": user_reported.get("vendor"),
        "date": user_reported.get("date"),
        "currency": user_reported.get("currency"),
    }

    if decision == "accept_ocr":
        for k, v in ocr_fields.items():
            base[k] = v
    elif decision == "keep_user_value":
        pass  # user_reported is already in base
    # For any decision: resolved_fields override either branch (covers "custom").
    for k, v in resolved.items():
        base[k] = v
    return base


def _source_per_field(
    audit_result: dict[str, Any], hitl_response: dict[str, Any]
) -> dict[str, str]:
    decision = hitl_response.get("decision", "accept_ocr")
    resolved = hitl_response.get("resolved_fields") or {}
    fields_in_conflict = {f["field"] for f in audit_result.get("fields_in_conflict", [])}
    user_reported = audit_result.get("user_reported") or {}
    out: dict[str, str] = {}
    for k in ("amount", "vendor", "date", "currency"):
        if k in resolved:
            out[k] = "hitl_custom"
        elif k in fields_in_conflict:
            out[k] = "ocr" if decision == "accept_ocr" else "user"
        elif user_reported.get(k) is not None:
            out[k] = "user"
        else:
            out[k] = "ocr"
    return out


def _from_ocr(extracted: dict[str, Any]) -> dict[str, Any]:
    return {
        "amount": _parse_ocr_amount((extracted.get("ocr_total") or {}).get("value")),
        "vendor": (extracted.get("ocr_vendor") or {}).get("value"),
        "date": _parse_ocr_date((extracted.get("ocr_date") or {}).get("value")),
    }


