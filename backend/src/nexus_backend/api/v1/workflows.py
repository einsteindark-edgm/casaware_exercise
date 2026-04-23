from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from nexus_backend.auth.dependencies import get_current_user
from nexus_backend.auth.models import CognitoUser
from nexus_backend.errors import NotAuthorized, ResourceNotFound
from nexus_backend.observability.logging import bind_request_context
from nexus_backend.observability.metrics import sse_connections_active
from nexus_backend.schemas.events import EventEnvelope
from nexus_backend.services.mongodb import mongo
from nexus_backend.services.sse_broker import sse_broker, workflow_channel
from nexus_backend.services.temporal_client import temporal_service

router = APIRouter(prefix="/workflows", tags=["workflows"])


async def _assert_workflow_belongs_to_tenant(workflow_id: str, tenant_id: str) -> None:
    doc = await mongo.db.expenses.find_one(
        {"workflow_id": workflow_id},
        {"_id": 0, "tenant_id": 1},
    )
    # Also consider chat workflows (RAG) which are not in the expenses collection.
    if doc is None:
        if workflow_id.startswith("rag-"):
            session_id = workflow_id.split("-")[1] if "-" in workflow_id else ""
            chat = await mongo.db.chat_sessions.find_one(
                {"session_id": session_id},
                {"_id": 0, "tenant_id": 1},
            )
            if chat is None:
                raise ResourceNotFound(f"workflow {workflow_id} not found")
            doc = chat
        else:
            raise ResourceNotFound(f"workflow {workflow_id} not found")

    if doc.get("tenant_id") != tenant_id:
        raise NotAuthorized("workflow belongs to a different tenant")


@router.get("/{workflow_id}/status")
async def get_status(
    workflow_id: str,
    user: CognitoUser = Depends(get_current_user),
) -> dict[str, Any]:
    await _assert_workflow_belongs_to_tenant(workflow_id, user.tenant_id)
    bind_request_context(workflow_id=workflow_id)
    result = await temporal_service.query(workflow_id, "get_status")
    return result or {"state": "unknown", "current_step": None}


def _serialise(event: EventEnvelope) -> ServerSentEvent:
    return ServerSentEvent(
        data=event.model_dump_json(),
        event=event.event_type,
        id=event.event_id,
        retry=3000,
    )


async def _stream(
    channel: str,
    request: Request,
    tenant_id: str,
    last_event_id: str | None,
) -> AsyncIterator[ServerSentEvent]:
    sse_connections_active.labels(tenant_id=tenant_id).inc()
    try:
        async for event in sse_broker.subscribe(
            channel, request, last_event_id=last_event_id
        ):
            yield _serialise(event)
            if event.event_type in {"workflow.completed", "workflow.failed"}:
                break
    finally:
        sse_connections_active.labels(tenant_id=tenant_id).dec()


@router.get("/{workflow_id}/stream")
async def stream_workflow(
    workflow_id: str,
    request: Request,
    user: CognitoUser = Depends(get_current_user),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> EventSourceResponse:
    await _assert_workflow_belongs_to_tenant(workflow_id, user.tenant_id)
    bind_request_context(workflow_id=workflow_id)
    channel = workflow_channel(user.tenant_id, workflow_id)
    return EventSourceResponse(
        _stream(channel, request, user.tenant_id, last_event_id),
        ping=15,
    )
