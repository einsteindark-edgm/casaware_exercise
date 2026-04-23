from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from nexus_backend.errors import ResourceNotFound
from nexus_backend.observability.logging import get_logger
from nexus_backend.services.temporal_client import TemporalBackend

log = get_logger(__name__)


@dataclass
class _WorkflowRecord:
    workflow_type: str
    workflow_id: str
    task_queue: str
    args: list[Any]
    memo: dict[str, Any] = field(default_factory=dict)
    signals: list[tuple[str, Any]] = field(default_factory=list)
    state: str = "running"
    current_step: str = "started"
    hitl_event: asyncio.Event = field(default_factory=asyncio.Event)
    hitl_payload: Any = None
    task: asyncio.Task | None = None


class FakeTemporalBackend(TemporalBackend):
    """In-memory Temporal backend that also *simulates* workflow side-effects.

    When ``start_workflow`` is called:
      * ExpenseAuditWorkflow publishes ocr_progress → hitl_required → (awaits
        hitl_response signal) → completed over the SSE broker.
      * RAGQueryWorkflow streams a few chat.token events then chat.complete.

    This lets the frontend exercise the happy path + HITL branch without the
    real Temporal workers (doc 03).
    """

    def __init__(self) -> None:
        self._by_id: dict[str, _WorkflowRecord] = {}
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def close(self) -> None:
        self._connected = False
        for rec in self._by_id.values():
            if rec.task and not rec.task.done():
                rec.task.cancel()

    async def ping(self) -> bool:
        return self._connected

    async def start_workflow(
        self,
        workflow_type: str,
        *,
        args: list[Any],
        workflow_id: str,
        task_queue: str,
        memo: dict[str, Any] | None = None,
    ) -> str:
        rec = _WorkflowRecord(
            workflow_type=workflow_type,
            workflow_id=workflow_id,
            task_queue=task_queue,
            args=list(args),
            memo=memo or {},
        )
        self._by_id[workflow_id] = rec

        if workflow_type == "ExpenseAuditWorkflow":
            rec.task = asyncio.create_task(self._simulate_expense_audit(rec))
        elif workflow_type == "RAGQueryWorkflow":
            rec.task = asyncio.create_task(self._simulate_rag_query(rec))
        return workflow_id

    async def signal(self, workflow_id: str, signal_name: str, payload: Any) -> None:
        rec = self._by_id.get(workflow_id)
        if rec is None:
            raise ResourceNotFound(f"workflow {workflow_id} not found")
        rec.signals.append((signal_name, payload))
        if signal_name == "hitl_response":
            rec.hitl_payload = payload
            rec.hitl_event.set()
            rec.current_step = "hitl_resolved"
        elif signal_name == "cancel_audit":
            rec.state = "failed"
            rec.current_step = "cancelled"
            if rec.task and not rec.task.done():
                rec.task.cancel()

    async def query(self, workflow_id: str, query_name: str) -> Any:
        rec = self._by_id.get(workflow_id)
        if rec is None:
            raise ResourceNotFound(f"workflow {workflow_id} not found")
        if query_name == "get_status":
            return {"state": rec.state, "current_step": rec.current_step}
        if query_name == "get_hitl_data":
            return None
        if query_name == "get_history":
            return [{"step": s, "timestamp": None, "result": None} for s, _ in rec.signals]
        return None

    def _dump(self) -> dict[str, Any]:
        """Debugging helper — not part of the TemporalBackend protocol."""
        return {wid: {k: v for k, v in rec.__dict__.items() if k != "task"}
                for wid, rec in self._by_id.items()}

    # ── Simulations ────────────────────────────────────────────────────────

    async def _simulate_expense_audit(self, rec: _WorkflowRecord) -> None:
        """Simulate the orchestrator lifecycle for ExpenseAuditWorkflow."""
        from nexus_backend.schemas.events import EventEnvelope
        from nexus_backend.services.mongodb import mongo
        from nexus_backend.services.sse_broker import (
            sse_broker,
            user_channel,
            workflow_channel,
        )
        from nexus_backend.ulid_ids import new_event_id, new_hitl_id

        if not rec.args or not isinstance(rec.args[0], dict):
            return
        input_ = rec.args[0]
        tenant_id = input_.get("tenant_id")
        user_id = input_.get("user_id")
        expense_id = input_.get("expense_id")
        if not (tenant_id and user_id and expense_id):
            return

        channels = [
            user_channel(tenant_id, user_id),
            workflow_channel(tenant_id, rec.workflow_id),
        ]

        async def record_event(
            event_type: str,
            details: dict[str, Any],
            actor: dict[str, Any] | None = None,
        ) -> None:
            try:
                await mongo.db.expense_events.insert_one(
                    {
                        "event_id": new_event_id(),
                        "expense_id": expense_id,
                        "tenant_id": tenant_id,
                        "event_type": event_type,
                        "actor": actor or {"type": "system", "id": "orchestrator"},
                        "details": details,
                        "workflow_id": rec.workflow_id,
                        "created_at": datetime.now(UTC),
                    }
                )
            except Exception as exc:
                log.warning("fake.event_persist_failed", error=str(exc), event_type=event_type)

        try:
            await asyncio.sleep(1.5)
            rec.current_step = "ocr"
            await record_event(
                "ocr_started",
                {"stage": "textract_running", "progress": 0.5},
            )
            await sse_broker.publish_many(
                channels,
                EventEnvelope(
                    event_type="workflow.ocr_progress",
                    workflow_id=rec.workflow_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    expense_id=expense_id,
                    payload={"stage": "textract_running", "progress": 0.5},
                ),
            )
            await asyncio.sleep(1.5)

            # Simulated OCR extracted values — pretend the amount is slightly off.
            reported = input_.get("user_reported_data") or {}
            reported_amount = float(reported.get("amount") or 0)
            ocr_amount = round(reported_amount + 0.5, 2) if reported_amount else 100.5

            # Decide whether HITL is required. For dev realism, always trigger it
            # so the user can exercise the resolver UI.
            needs_hitl = True

            if needs_hitl:
                hitl_task_id = new_hitl_id()
                now = datetime.now(UTC)
                fields_in_conflict = [
                    {
                        "field": "amount",
                        "user_value": reported_amount,
                        "ocr_value": ocr_amount,
                        "confidence": 0.92,
                    }
                ]
                try:
                    await mongo.db.hitl_tasks.insert_one(
                        {
                            "task_id": hitl_task_id,
                            "tenant_id": tenant_id,
                            "user_id": user_id,
                            "expense_id": expense_id,
                            "workflow_id": rec.workflow_id,
                            "status": "pending",
                            "fields_in_conflict": fields_in_conflict,
                            "created_at": now,
                        }
                    )
                    await mongo.db.expenses.update_one(
                        {"expense_id": expense_id, "tenant_id": tenant_id},
                        {"$set": {"status": "hitl_required", "updated_at": now}},
                    )
                except Exception as exc:
                    log.warning("fake.hitl_persist_failed", error=str(exc))

                rec.current_step = "hitl_required"
                await record_event(
                    "ocr_completed",
                    {"extracted_amount": ocr_amount, "confidence": 0.92},
                )
                await record_event(
                    "hitl_required",
                    {
                        "hitl_task_id": hitl_task_id,
                        "fields_in_conflict": fields_in_conflict,
                    },
                )
                await sse_broker.publish_many(
                    channels,
                    EventEnvelope(
                        event_type="workflow.hitl_required",
                        workflow_id=rec.workflow_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        expense_id=expense_id,
                        payload={
                            "hitl_task_id": hitl_task_id,
                            "fields_in_conflict": fields_in_conflict,
                        },
                    ),
                )

                try:
                    await asyncio.wait_for(rec.hitl_event.wait(), timeout=600)
                except TimeoutError:
                    rec.state = "failed"
                    rec.current_step = "hitl_timeout"
                    await record_event("failed", {"reason": "hitl_timeout"})
                    await sse_broker.publish_many(
                        channels,
                        EventEnvelope(
                            event_type="workflow.failed",
                            workflow_id=rec.workflow_id,
                            tenant_id=tenant_id,
                            user_id=user_id,
                            expense_id=expense_id,
                            payload={"reason": "hitl_timeout"},
                        ),
                    )
                    return

            await asyncio.sleep(0.8)
            rec.state = "completed"
            rec.current_step = "completed"
            now = datetime.now(UTC)
            try:
                await mongo.db.expenses.update_one(
                    {"expense_id": expense_id, "tenant_id": tenant_id},
                    {"$set": {"status": "approved", "updated_at": now}},
                )
            except Exception as exc:
                log.warning("fake.complete_persist_failed", error=str(exc))

            await record_event("completed", {"final_status": "approved"})
            await sse_broker.publish_many(
                channels,
                EventEnvelope(
                    event_type="workflow.completed",
                    workflow_id=rec.workflow_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    expense_id=expense_id,
                    payload={"final_status": "approved"},
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("fake.simulation_error", error=str(exc))

    async def _simulate_rag_query(self, rec: _WorkflowRecord) -> None:
        from nexus_backend.schemas.events import EventEnvelope
        from nexus_backend.services.sse_broker import (
            sse_broker,
            user_channel,
            workflow_channel,
        )

        if not rec.args or not isinstance(rec.args[0], dict):
            return
        input_ = rec.args[0]
        tenant_id = input_.get("tenant_id")
        user_id = input_.get("user_id")
        if not (tenant_id and user_id):
            return

        channels = [
            user_channel(tenant_id, user_id),
            workflow_channel(tenant_id, rec.workflow_id),
        ]
        answer = "Según tus gastos recientes, Starbucks suma $15.50 USD este mes."
        try:
            await asyncio.sleep(0.3)
            for word in answer.split(" "):
                await sse_broker.publish_many(
                    channels,
                    EventEnvelope(
                        event_type="chat.token",
                        workflow_id=rec.workflow_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        payload={"token": word + " "},
                    ),
                )
                await asyncio.sleep(0.08)

            rec.state = "completed"
            rec.current_step = "completed"
            await sse_broker.publish_many(
                channels,
                EventEnvelope(
                    event_type="chat.complete",
                    workflow_id=rec.workflow_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    payload={
                        "citations": [
                            {"expense_id": "exp_fake", "snippet": "Starbucks $15.50"}
                        ]
                    },
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("fake.rag_simulation_error", error=str(exc))
