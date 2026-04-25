"""Integration-style workflow tests using Temporal's local time-skipping env.

Activities are replaced by in-memory mocks registered under the same names the
workflows use. `compare_fields` is the real implementation.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from nexus_orchestration.activities.comparison import compare_fields
from nexus_orchestration.workflows.audit_validation import AuditValidationWorkflow
from nexus_orchestration.workflows.expense_audit import ExpenseAuditWorkflow
from nexus_orchestration.workflows.ocr_extraction import OCRExtractionWorkflow


@activity.defn(name="publish_event")
async def mock_publish_event(event: dict[str, Any]) -> None:
    return None


@activity.defn(name="emit_expense_event")
async def mock_emit_expense_event(event: dict[str, Any]) -> None:
    return None


@activity.defn(name="update_expense_status")
async def mock_update_expense_status(inp: dict[str, Any]) -> None:
    return None


@activity.defn(name="update_expense_to_approved")
async def mock_update_expense_to_approved(inp: dict[str, Any]) -> None:
    return None


@activity.defn(name="update_expense_to_rejected")
async def mock_update_expense_to_rejected(inp: dict[str, Any]) -> None:
    return None


@activity.defn(name="textract_analyze_expense")
async def mock_textract(inp: dict[str, Any]) -> dict[str, Any]:
    # Force a discrepancy: user amount=100, OCR=120 → amount_pct beyond 1%
    return {
        "raw_output_s3_key": "fake/key.json",
        "fields": {
            "ocr_total": {"value": 120.0, "confidence": 99.0},
            "ocr_vendor": {"value": "Starbucks", "confidence": 96.0},
            "ocr_date": {"value": "2026-04-22", "confidence": 91.0},
        },
        "avg_confidence": 95.0,
        "fields_summary": [],
    }


@activity.defn(name="textract_analyze_document_queries")
async def mock_textract_queries(inp: dict[str, Any]) -> dict[str, Any]:
    return await mock_textract(inp)


@activity.defn(name="upsert_ocr_extraction")
async def mock_upsert_ocr(inp: dict[str, Any]) -> None:
    return None


@activity.defn(name="query_expense_for_validation")
async def mock_query_expense(inp: dict[str, Any]) -> dict[str, Any]:
    return {
        "amount": 100.0,
        "currency": "COP",
        "vendor": "Starbucks",
        "date": "2026-04-22",
    }


@activity.defn(name="create_hitl_task")
async def mock_create_hitl(inp: dict[str, Any]) -> str:
    return f"hitl_test_{inp['expense_id']}"


@activity.defn(name="trigger_vector_sync")
async def mock_trigger_vector_sync(inp: dict[str, Any]) -> dict[str, Any]:
    return {"status": "skipped_fake"}


ALL_MOCK_ACTIVITIES = [
    mock_publish_event,
    mock_emit_expense_event,
    mock_update_expense_status,
    mock_update_expense_to_approved,
    mock_update_expense_to_rejected,
    mock_textract,
    mock_textract_queries,
    mock_upsert_ocr,
    mock_query_expense,
    mock_create_hitl,
    mock_trigger_vector_sync,
    compare_fields,
]


@pytest.mark.asyncio
async def test_expense_audit_hitl_accept_ocr():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="nexus-orchestrator-tq",
            workflows=[ExpenseAuditWorkflow, OCRExtractionWorkflow, AuditValidationWorkflow],
            activities=ALL_MOCK_ACTIVITIES,
        ):
            async with Worker(
                env.client,
                task_queue="nexus-ocr-tq",
                workflows=[OCRExtractionWorkflow],
                activities=ALL_MOCK_ACTIVITIES,
            ):
                async with Worker(
                    env.client,
                    task_queue="nexus-databricks-tq",
                    workflows=[AuditValidationWorkflow],
                    activities=ALL_MOCK_ACTIVITIES,
                ):
                    expense_id = f"exp_{uuid.uuid4().hex[:8]}"
                    handle = await env.client.start_workflow(
                        "ExpenseAuditWorkflow",
                        {
                            "expense_id": expense_id,
                            "tenant_id": "t_ACME",
                            "user_id": "u_1",
                            "receipt_s3_key": "tenant=t_ACME/user=u_1/x.pdf",
                            "user_reported_data": {
                                "amount": 100.0,
                                "currency": "COP",
                                "vendor": "Starbucks",
                                "date": "2026-04-22",
                            },
                        },
                        id=f"expense-audit-{expense_id}",
                        task_queue="nexus-orchestrator-tq",
                    )

                    # Wait for HITL gate then signal the resolution.
                    import asyncio

                    for _ in range(20):
                        status = await handle.query("get_status")
                        if status["state"] == "waiting_hitl":
                            break
                        await asyncio.sleep(0.2)

                    await handle.signal(
                        "hitl_response",
                        {
                            "decision": "accept_ocr",
                            "resolved_fields": {},
                            "user_id": "u_1",
                            "timestamp": "2026-04-22T00:00:00Z",
                        },
                    )
                    result = await handle.result()
                    assert result["status"] == "completed"
                    assert result["final_data"]["amount"] == 120.0  # OCR value


@pytest.mark.asyncio
async def test_expense_audit_cancel_signal():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="nexus-orchestrator-tq",
            workflows=[ExpenseAuditWorkflow, OCRExtractionWorkflow, AuditValidationWorkflow],
            activities=ALL_MOCK_ACTIVITIES,
        ), Worker(
            env.client,
            task_queue="nexus-ocr-tq",
            workflows=[OCRExtractionWorkflow],
            activities=ALL_MOCK_ACTIVITIES,
        ), Worker(
            env.client,
            task_queue="nexus-databricks-tq",
            workflows=[AuditValidationWorkflow],
            activities=ALL_MOCK_ACTIVITIES,
        ):
            expense_id = f"exp_{uuid.uuid4().hex[:8]}"
            handle = await env.client.start_workflow(
                "ExpenseAuditWorkflow",
                {
                    "expense_id": expense_id,
                    "tenant_id": "t_ACME",
                    "user_id": "u_1",
                    "receipt_s3_key": "x.pdf",
                    "user_reported_data": {
                        "amount": 100.0,
                        "vendor": "Starbucks",
                        "date": "2026-04-22",
                        "currency": "COP",
                    },
                },
                id=f"expense-audit-{expense_id}",
                task_queue="nexus-orchestrator-tq",
            )

            import asyncio

            for _ in range(20):
                status = await handle.query("get_status")
                if status["state"] == "waiting_hitl":
                    break
                await asyncio.sleep(0.2)

            await handle.signal("cancel_audit", {"reason": "user_cancel", "user_id": "u_1"})
            result = await handle.result()
            assert result["status"] == "cancelled"


@pytest.mark.asyncio
async def test_expense_audit_hitl_timeout():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="nexus-orchestrator-tq",
            workflows=[ExpenseAuditWorkflow, OCRExtractionWorkflow, AuditValidationWorkflow],
            activities=ALL_MOCK_ACTIVITIES,
        ), Worker(
            env.client,
            task_queue="nexus-ocr-tq",
            workflows=[OCRExtractionWorkflow],
            activities=ALL_MOCK_ACTIVITIES,
        ), Worker(
            env.client,
            task_queue="nexus-databricks-tq",
            workflows=[AuditValidationWorkflow],
            activities=ALL_MOCK_ACTIVITIES,
        ):
            expense_id = f"exp_{uuid.uuid4().hex[:8]}"
            handle = await env.client.start_workflow(
                "ExpenseAuditWorkflow",
                {
                    "expense_id": expense_id,
                    "tenant_id": "t_ACME",
                    "user_id": "u_1",
                    "receipt_s3_key": "x.pdf",
                    "user_reported_data": {
                        "amount": 100.0,
                        "vendor": "Starbucks",
                        "date": "2026-04-22",
                        "currency": "COP",
                    },
                },
                id=f"expense-audit-{expense_id}",
                task_queue="nexus-orchestrator-tq",
            )

            # Skip past the 7-day HITL timeout.
            await env.sleep(timedelta(days=8))
            result = await handle.result()
            assert result["status"] == "failed"
            assert result["reason"] == "HITL_TIMEOUT"


_ = WorkflowFailureError  # reserved for a future negative-path test
