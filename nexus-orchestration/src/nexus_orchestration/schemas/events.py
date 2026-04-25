"""Event envelope — MUST stay byte-compatible with backend/src/nexus_backend/schemas/events.py.

Worker emits events through Redis; the backend consumes them via SSE. The two
systems never import each other; the contract test in
`tests/integration/test_contract_schemas.py` asserts the schemas are identical.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from nexus_orchestration.ulid_ids import epoch_ms_now, new_event_id, ulid_to_epoch_ms

EventType = Literal[
    "workflow.started",
    "workflow.step_changed",
    "workflow.ocr_progress",
    "workflow.hitl_required",
    "workflow.hitl_resolved",
    "workflow.completed",
    "workflow.failed",
    "chat.token",
    "chat.complete",
    "ping",
]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class EventEnvelope(BaseModel):
    """Canonical event shape published to Redis (contract 00 §2.3)."""

    schema_version: str = "1.0"
    event_id: str = Field(default_factory=new_event_id)
    event_type: EventType
    workflow_id: str | None = None
    tenant_id: str
    user_id: str | None = None
    expense_id: str | None = None
    timestamp: str = Field(default_factory=_now_iso)
    payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def epoch_ms(self) -> int:
        try:
            return ulid_to_epoch_ms(self.event_id)
        except Exception:
            return epoch_ms_now()
