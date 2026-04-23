from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, Request
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from nexus_backend.auth.dependencies import get_current_user
from nexus_backend.auth.models import CognitoUser
from nexus_backend.observability.metrics import sse_connections_active
from nexus_backend.schemas.events import EventEnvelope
from nexus_backend.services.sse_broker import sse_broker, user_channel

router = APIRouter(prefix="/events", tags=["events"])


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
    finally:
        sse_connections_active.labels(tenant_id=tenant_id).dec()


def _serialise(event: EventEnvelope) -> ServerSentEvent:
    return ServerSentEvent(
        data=event.model_dump_json(),
        event=event.event_type,
        id=event.event_id,
        retry=3000,
    )


@router.get("/stream")
async def stream_events(
    request: Request,
    user: CognitoUser = Depends(get_current_user),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> EventSourceResponse:
    channel = user_channel(user.tenant_id, user.sub)
    return EventSourceResponse(
        _stream(channel, request, user.tenant_id, last_event_id),
        ping=15,
    )
