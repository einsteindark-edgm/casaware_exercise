"""AuditValidationWorkflow — compares OCR extraction against user-reported data.

Runs in the `nexus-databricks-tq` queue. In dev the "databricks" read is a
Mongo query; the activity itself decides.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn(name="AuditValidationWorkflow")
class AuditValidationWorkflow:
    @workflow.run
    async def run(self, inp: dict[str, Any]) -> dict[str, Any]:
        expense_input = inp["expense_input"]
        ocr_result = inp["ocr_result"]
        expense_id = expense_input["expense_id"]
        tenant_id = expense_input["tenant_id"]
        user_id = expense_input["user_id"]

        # 1) Read user-reported data from the authoritative source.
        user_reported = await workflow.execute_activity(
            "query_expense_for_validation",
            {"expense_id": expense_id, "tenant_id": tenant_id},
            start_to_close_timeout=timedelta(seconds=15),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

        # 2) Compare (pure function, no LLM). Pure & deterministic → retrying on
        # failure can't help, cap to 2 attempts so a permanent bug (like the
        # "could not convert string to float: '24,395.00 COP'" regression we
        # hit in prod) fails the workflow fast instead of looping for hours
        # and flooding the worker log.
        comparison = await workflow.execute_activity(
            "compare_fields",
            {
                "user_reported": user_reported,
                "ocr_extracted": ocr_result["fields"],
                "tolerance": {"amount_pct": 0.01, "vendor_similarity_min": 0.85},
            },
            start_to_close_timeout=timedelta(seconds=5),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )

        parent_workflow_id = _parent_workflow_id()

        hitl_task_id: str | None = None
        fields_in_conflict = comparison["fields_in_conflict"]
        if fields_in_conflict:
            hitl_task_id = await workflow.execute_activity(
                "create_hitl_task",
                {
                    "workflow_id": parent_workflow_id,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "expense_id": expense_id,
                    "fields_in_conflict": fields_in_conflict,
                },
                start_to_close_timeout=timedelta(seconds=10),
            )
            await workflow.execute_activity(
                "emit_expense_event",
                {
                    "expense_id": expense_id,
                    "tenant_id": tenant_id,
                    "event_type": "discrepancy_detected",
                    "actor": {"type": "system", "id": "worker:auditor"},
                    "details": {
                        "fields_in_conflict": fields_in_conflict,
                        "hitl_task_id": hitl_task_id,
                    },
                },
                start_to_close_timeout=timedelta(seconds=5),
            )
        else:
            await workflow.execute_activity(
                "emit_expense_event",
                {
                    "expense_id": expense_id,
                    "tenant_id": tenant_id,
                    "event_type": "no_discrepancy",
                    "actor": {"type": "system", "id": "worker:auditor"},
                    "details": {},
                },
                start_to_close_timeout=timedelta(seconds=5),
            )

        return {
            "has_discrepancies": bool(fields_in_conflict),
            "fields_in_conflict": fields_in_conflict,
            "hitl_task_id": hitl_task_id,
            "extracted_data": ocr_result["fields"],
            "user_reported": user_reported,
        }


def _parent_workflow_id() -> str:
    info = workflow.info()
    parent = getattr(info, "parent", None)
    if parent is not None:
        return parent.workflow_id
    return info.workflow_id
