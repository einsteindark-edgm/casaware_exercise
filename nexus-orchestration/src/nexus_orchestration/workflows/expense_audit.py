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

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

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

        # 5) Redis completion event.
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
        "amount": (extracted.get("ocr_total") or {}).get("value"),
        "vendor": (extracted.get("ocr_vendor") or {}).get("value"),
        "date": (extracted.get("ocr_date") or {}).get("value"),
    }


