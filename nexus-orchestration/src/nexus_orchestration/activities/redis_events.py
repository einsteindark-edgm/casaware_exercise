"""Publish an EventEnvelope to Redis.

Must match the exact buffer protocol the backend's SSE broker uses so replay
works (backend/src/nexus_backend/services/sse_broker.py):

  pipeline:
    publish(channel, payload)
    zadd({channel}:buffer, {payload: epoch_ms})
    zremrangebyrank({channel}:buffer, 0, -51)   # keep last 50
    expire({channel}:buffer, 3600)
"""
from __future__ import annotations

from typing import Any

import redis.asyncio as redis
from temporalio import activity

from nexus_orchestration.config import settings
from nexus_orchestration.schemas.events import EventEnvelope

_BUFFER_CAP = 50
_BUFFER_TTL_SECONDS = 3600

_client: redis.Redis | None = None


def _get_client() -> redis.Redis:
    global _client
    if _client is None:
        kwargs: dict[str, Any] = {
            "encoding": "utf-8",
            "decode_responses": True,
            "health_check_interval": 30,
        }
        if settings.redis_tls:
            kwargs["ssl"] = True
        _client = redis.from_url(settings.redis_url, **kwargs)
    return _client


def user_channel(tenant_id: str, user_id: str) -> str:
    return f"nexus:events:tenant:{tenant_id}:user:{user_id}"


def workflow_channel(tenant_id: str, workflow_id: str) -> str:
    return f"nexus:events:tenant:{tenant_id}:workflow:{workflow_id}"


async def _publish_to_channel(client: redis.Redis, channel: str, envelope: EventEnvelope) -> None:
    payload = envelope.model_dump_json()
    async with client.pipeline(transaction=False) as pipe:
        pipe.publish(channel, payload)
        pipe.zadd(f"{channel}:buffer", {payload: envelope.epoch_ms})
        pipe.zremrangebyrank(f"{channel}:buffer", 0, -(_BUFFER_CAP + 1))
        pipe.expire(f"{channel}:buffer", _BUFFER_TTL_SECONDS)
        await pipe.execute()


@activity.defn(name="publish_event")
async def publish_event(event: dict[str, Any]) -> None:
    """Fan-out publish to both user and workflow channels.

    Input shape (matches doc 03 §4):
      {
        event_type, tenant_id, user_id?, expense_id?,
        workflow_id? (falls back to activity.info().workflow_id),
        payload
      }
    """
    workflow_id = event.get("workflow_id") or activity.info().workflow_id

    envelope = EventEnvelope(
        event_type=event["event_type"],
        workflow_id=workflow_id,
        tenant_id=event["tenant_id"],
        user_id=event.get("user_id"),
        expense_id=event.get("expense_id"),
        payload=event.get("payload", {}),
    )

    client = _get_client()
    tenant_id = event["tenant_id"]

    # User channel (personal feed)
    if event.get("user_id"):
        await _publish_to_channel(client, user_channel(tenant_id, event["user_id"]), envelope)

    # Workflow channel (per-workflow subscribers)
    if workflow_id:
        await _publish_to_channel(client, workflow_channel(tenant_id, workflow_id), envelope)
