"""Contract test: the worker's EventEnvelope schema MUST match the backend's.

Any divergence (new field, renamed field, changed default) breaks the SSE
consumer because the envelope ships across process boundaries via Redis as
JSON. This test parses both schemas byte-by-byte and fails loudly.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from nexus_orchestration.schemas.events import EventEnvelope as WorkerEnvelope

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BACKEND_SRC = _REPO_ROOT / "backend" / "src"
_BACKEND_EVENTS_PATH = _BACKEND_SRC / "nexus_backend" / "schemas" / "events.py"


def _load_backend_envelope():
    # Add backend/src to sys.path once so its imports (ulid_ids) resolve.
    # The backend's events.py only depends on python-ulid, pydantic, and its
    # own ulid_ids module — all safe to import in the worker test env.
    if str(_BACKEND_SRC) not in sys.path:
        sys.path.insert(0, str(_BACKEND_SRC))
    from nexus_backend.schemas.events import EventEnvelope  # type: ignore[import-not-found]

    return EventEnvelope


@pytest.mark.skipif(
    not _BACKEND_EVENTS_PATH.exists(),
    reason="backend schemas not checked out — skip contract test",
)
def test_event_envelope_schemas_match():
    BackendEnvelope = _load_backend_envelope()

    worker_schema = WorkerEnvelope.model_json_schema()
    backend_schema = BackendEnvelope.model_json_schema()

    # Compare the structural parts. Title differs (class lives in different
    # modules) but everything that matters must match exactly.
    def _structural(schema: dict) -> dict:
        return {
            "properties": schema.get("properties"),
            "required": schema.get("required"),
            "type": schema.get("type"),
        }

    assert _structural(worker_schema) == _structural(backend_schema), (
        "EventEnvelope drift between worker and backend. "
        "If you changed one, update the other AND doc 00-contratos-compartidos.md §2.3."
    )


@pytest.mark.skipif(
    not _BACKEND_EVENTS_PATH.exists(),
    reason="backend schemas not checked out — skip contract test",
)
def test_event_types_superset_of_worker():
    """Backend must know every event_type the worker emits."""
    BackendEnvelope = _load_backend_envelope()

    # Worker emits these (see workflows + redis_events).
    worker_emits = {
        "workflow.ocr_progress",
        "workflow.hitl_required",
        "workflow.completed",
        "workflow.failed",
        "chat.token",
        "chat.complete",
    }

    backend_schema = BackendEnvelope.model_json_schema()
    # event_type is a Literal — its enum values live under $defs or inline.
    event_type_def = backend_schema["properties"]["event_type"]
    allowed = set(event_type_def.get("enum") or [])
    if not allowed:
        # Follow $ref if present.
        ref = event_type_def.get("$ref", "")
        key = ref.rsplit("/", 1)[-1]
        allowed = set(backend_schema.get("$defs", {}).get(key, {}).get("enum", []))

    missing = worker_emits - allowed
    assert not missing, f"Backend's EventType literal is missing: {missing}"
