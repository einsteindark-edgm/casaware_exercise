"""Mongo writes owned by the worker.

The backend owns: `expenses` create, `receipts`, `expense_events` of type
"created"/"hitl_resolved", `hitl_tasks.status=resolved` transition. Everything
else is the worker's: `ocr_extractions`, `hitl_tasks` creation, transitional
`expense_events`, status transitions on `expenses`, and `chat_turns`.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from temporalio import activity

from nexus_orchestration.config import settings
from nexus_orchestration.observability.otel import current_trace_id_hex
from nexus_orchestration.ulid_ids import new_event_id, new_hitl_id

_client: AsyncIOMotorClient | None = None


def _db() -> AsyncIOMotorDatabase:
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(settings.mongodb_uri, tz_aware=True)
    return _client[settings.mongodb_db]


def _now() -> datetime:
    return datetime.now(UTC)


@activity.defn(name="upsert_ocr_extraction")
async def upsert_ocr_extraction(inp: dict[str, Any]) -> None:
    """Persists the normalised OCR result. CDC replicates it to silver.ocr_extractions."""
    doc = {
        "tenant_id": inp["tenant_id"],
        "user_id": inp.get("user_id"),
        "ocr_total": (inp.get("ocr_total") or {}).get("value"),
        "ocr_total_confidence": (inp.get("ocr_total") or {}).get("confidence"),
        "ocr_vendor": (inp.get("ocr_vendor") or {}).get("value"),
        "ocr_vendor_confidence": (inp.get("ocr_vendor") or {}).get("confidence"),
        "ocr_date": (inp.get("ocr_date") or {}).get("value"),
        "ocr_date_confidence": (inp.get("ocr_date") or {}).get("confidence"),
        "avg_confidence": inp.get("avg_confidence"),
        "textract_raw_s3_key": inp.get("textract_raw_s3_key"),
        "extracted_by_workflow_id": activity.info().workflow_id,
        "extracted_at": _now(),
    }
    await _db().ocr_extractions.update_one(
        {"expense_id": inp["expense_id"]}, {"$set": doc}, upsert=True
    )


@activity.defn(name="update_expense_status")
async def update_expense_status(inp: dict[str, Any]) -> None:
    """Updates only the status field. Used for intermediate transitions."""
    await _db().expenses.update_one(
        {"expense_id": inp["expense_id"], "tenant_id": inp["tenant_id"]},
        {"$set": {"status": inp["status"], "updated_at": _now()}},
    )


@activity.defn(name="update_expense_to_approved")
async def update_expense_to_approved(inp: dict[str, Any]) -> None:
    """Final approval write. Lakeflow picks this up via CDC and promotes to Gold."""
    final = inp["final_data"]
    now = _now()
    await _db().expenses.update_one(
        {"expense_id": inp["expense_id"], "tenant_id": inp["tenant_id"]},
        {
            "$set": {
                "status": "approved",
                "final_amount": final.get("amount"),
                "final_vendor": final.get("vendor"),
                "final_date": final.get("date"),
                "final_currency": final.get("currency"),
                "source_per_field": inp["source_per_field"],
                "approved_at": now,
                "updated_at": now,
            }
        },
    )


@activity.defn(name="update_expense_to_rejected")
async def update_expense_to_rejected(inp: dict[str, Any]) -> None:
    now = _now()
    await _db().expenses.update_one(
        {"expense_id": inp["expense_id"], "tenant_id": inp["tenant_id"]},
        {
            "$set": {
                "status": "rejected",
                "rejection_reason": inp.get("reason"),
                "updated_at": now,
            }
        },
    )


@activity.defn(name="emit_expense_event")
async def emit_expense_event(inp: dict[str, Any]) -> None:
    """Append an entry to expense_events (the semantic timeline)."""
    metadata: dict[str, Any] = dict(inp.get("metadata") or {})
    trace_id = current_trace_id_hex()
    if trace_id and "trace_id" not in metadata:
        metadata["trace_id"] = trace_id
    await _db().expense_events.insert_one(
        {
            "event_id": new_event_id(),
            "expense_id": inp["expense_id"],
            "tenant_id": inp["tenant_id"],
            "event_type": inp["event_type"],
            "actor": inp.get("actor") or {"type": "system", "id": "worker"},
            "details": inp.get("details", {}),
            "workflow_id": activity.info().workflow_id,
            "metadata": metadata,
            "created_at": _now(),
        }
    )


@activity.defn(name="create_hitl_task")
async def create_hitl_task(inp: dict[str, Any]) -> str:
    task_id = new_hitl_id()
    await _db().hitl_tasks.insert_one(
        {
            "task_id": task_id,
            "workflow_id": inp["workflow_id"],
            "tenant_id": inp["tenant_id"],
            "user_id": inp["user_id"],
            "expense_id": inp["expense_id"],
            "discrepancy_fields": inp["fields_in_conflict"],
            "status": "pending",
            "created_at": _now(),
        }
    )
    return task_id


@activity.defn(name="save_chat_turn")
async def save_chat_turn(inp: dict[str, Any]) -> None:
    """Persist a completed chat turn so later turns can read history."""
    db = _db()
    # Ensure the history index exists (cheap; motor caches the command result).
    await db.chat_turns.create_index(
        [("session_id", 1), ("turn", 1)], unique=True, background=True
    )
    await db.chat_turns.update_one(
        {"session_id": inp["session_id"], "turn": inp["turn"]},
        {
            "$set": {
                "tenant_id": inp["tenant_id"],
                "user_id": inp["user_id"],
                "user_message": inp["user_message"],
                "assistant_message": inp["assistant_message"],
                "citations": inp.get("citations", []),
                "created_at": _now(),
            }
        },
        upsert=True,
    )


@activity.defn(name="load_chat_history")
async def load_chat_history(inp: dict[str, Any]) -> list[dict[str, Any]]:
    """Load prior turns as a flat list of {role, content} messages for Bedrock."""
    cursor = (
        _db()
        .chat_turns.find(
            {"session_id": inp["session_id"], "tenant_id": inp["tenant_id"]},
            {"_id": 0, "user_message": 1, "assistant_message": 1, "turn": 1},
        )
        .sort("turn", 1)
        .limit(inp.get("max_turns", 10))
    )
    turns = await cursor.to_list(length=inp.get("max_turns", 10))
    messages: list[dict[str, Any]] = []
    for t in turns:
        messages.append({"role": "user", "content": [{"text": t["user_message"]}]})
        messages.append({"role": "assistant", "content": [{"text": t["assistant_message"]}]})
    return messages
