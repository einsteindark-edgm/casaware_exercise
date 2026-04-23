from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, Request
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from nexus_backend.auth.dependencies import get_current_user
from nexus_backend.auth.models import CognitoUser
from nexus_backend.errors import NotAuthorized, ResourceNotFound
from nexus_backend.observability.logging import bind_request_context
from nexus_backend.schemas.chat import ChatStartRequest, ChatStartResponse
from nexus_backend.schemas.events import EventEnvelope
from nexus_backend.services.mongodb import mongo
from nexus_backend.services.sse_broker import sse_broker, workflow_channel
from nexus_backend.services.temporal_client import temporal_service
from nexus_backend.ulid_ids import new_session_id

router = APIRouter(prefix="/chat", tags=["chat"])

_STREAM_EVENT_TYPES = {"chat.token", "chat.complete"}


@router.post("", response_model=ChatStartResponse)
async def start_chat(
    body: ChatStartRequest,
    user: CognitoUser = Depends(get_current_user),
) -> ChatStartResponse:
    now = datetime.now(UTC)
    if body.session_id:
        session = await mongo.db.chat_sessions.find_one({"session_id": body.session_id})
        if session is None:
            raise ResourceNotFound(f"chat session {body.session_id} not found")
        if session.get("tenant_id") != user.tenant_id:
            raise NotAuthorized("chat session belongs to a different tenant")
        session_id = body.session_id
        turn = int(session.get("turn_count", 0)) + 1
        await mongo.db.chat_sessions.update_one(
            {"session_id": session_id},
            {"$set": {"turn_count": turn, "updated_at": now}},
        )
    else:
        session_id = new_session_id()
        turn = 1
        await mongo.db.chat_sessions.insert_one(
            {
                "session_id": session_id,
                "tenant_id": user.tenant_id,
                "user_id": user.sub,
                "turn_count": turn,
                "created_at": now,
                "updated_at": now,
            }
        )

    workflow_id = f"rag-{session_id}-{turn}"
    bind_request_context(workflow_id=workflow_id)

    await temporal_service.start_workflow(
        "RAGQueryWorkflow",
        args=[
            {
                "session_id": session_id,
                "turn": turn,
                "tenant_id": user.tenant_id,
                "user_id": user.sub,
                "message": body.message,
            }
        ],
        workflow_id=workflow_id,
        task_queue="nexus-rag-tq",
    )

    return ChatStartResponse(session_id=session_id, workflow_id=workflow_id, turn=turn)


async def _stream(
    channel: str,
    request: Request,
    last_event_id: str | None,
) -> AsyncIterator[ServerSentEvent]:
    async for event in sse_broker.subscribe(channel, request, last_event_id=last_event_id):
        if event.event_type in _STREAM_EVENT_TYPES or event.event_type == "ping":
            yield ServerSentEvent(
                data=event.model_dump_json(),
                event=event.event_type,
                id=event.event_id,
                retry=3000,
            )
        if event.event_type == "chat.complete":
            break


@router.get("/stream/{workflow_id}")
async def stream_chat(
    workflow_id: str,
    request: Request,
    user: CognitoUser = Depends(get_current_user),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> EventSourceResponse:
    if not workflow_id.startswith("rag-"):
        raise NotAuthorized("not a chat workflow id")

    parts = workflow_id.split("-")
    if len(parts) < 3:
        raise NotAuthorized("malformed chat workflow id")
    session_id = parts[1]
    session = await mongo.db.chat_sessions.find_one({"session_id": session_id})
    if session is None:
        raise ResourceNotFound(f"chat session {session_id} not found")
    if session.get("tenant_id") != user.tenant_id:
        raise NotAuthorized("chat session belongs to a different tenant")

    channel = workflow_channel(user.tenant_id, workflow_id)
    return EventSourceResponse(_stream(channel, request, last_event_id), ping=15)


__all__ = ["router"]

_ = EventEnvelope  # referenced indirectly through broker
