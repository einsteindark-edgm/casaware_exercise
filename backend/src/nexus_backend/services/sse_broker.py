from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

from fastapi import Request

from nexus_backend.observability.logging import get_logger
from nexus_backend.observability.metrics import sse_events_published
from nexus_backend.schemas.events import EventEnvelope
from nexus_backend.services.redis_client import redis_pool
from nexus_backend.ulid_ids import epoch_ms_now, ulid_to_epoch_ms

log = get_logger(__name__)

_BUFFER_CAP = 50
_BUFFER_TTL_SECONDS = 3600
_HEARTBEAT_SECONDS = 15


def user_channel(tenant_id: str, user_id: str) -> str:
    return f"nexus:events:tenant:{tenant_id}:user:{user_id}"


def workflow_channel(tenant_id: str, workflow_id: str) -> str:
    return f"nexus:events:tenant:{tenant_id}:workflow:{workflow_id}"


class SSEBroker:
    """Redis-backed pub/sub broker with replay buffer for SSE resilience."""

    async def publish(self, channel: str, event: EventEnvelope) -> None:
        client = redis_pool.client
        payload = event.model_dump_json()

        async with client.pipeline(transaction=False) as pipe:
            pipe.publish(channel, payload)
            pipe.zadd(f"{channel}:buffer", {payload: event.epoch_ms})
            pipe.zremrangebyrank(f"{channel}:buffer", 0, -(_BUFFER_CAP + 1))
            pipe.expire(f"{channel}:buffer", _BUFFER_TTL_SECONDS)
            await pipe.execute()

        sse_events_published.labels(event_type=event.event_type).inc()

    async def publish_many(self, channels: list[str], event: EventEnvelope) -> None:
        for ch in channels:
            await self.publish(ch, event)

    async def subscribe(
        self,
        channel: str,
        request: Request,
        *,
        last_event_id: str | None = None,
    ) -> AsyncIterator[EventEnvelope]:
        client = redis_pool.client

        # 1) Replay any events the client missed.
        if last_event_id:
            try:
                min_score = ulid_to_epoch_ms(last_event_id)
            except Exception:
                min_score = 0
            # Exclusive lower bound: use "(" prefix per Redis ZRANGEBYSCORE semantics.
            replay = await client.zrangebyscore(
                f"{channel}:buffer",
                min=f"({min_score}",
                max=str(epoch_ms_now()),
            )
            for raw in replay:
                try:
                    yield EventEnvelope.model_validate_json(raw)
                except Exception as exc:
                    log.warning("sse.replay_parse_failed", error=str(exc))

        # 2) Live pub/sub loop.
        pubsub = client.pubsub(ignore_subscribe_messages=True)
        await pubsub.subscribe(channel)
        last_heartbeat = time.monotonic()
        try:
            while True:
                if await request.is_disconnected():
                    break

                msg = await pubsub.get_message(timeout=1.0)
                if msg and msg.get("type") == "message":
                    try:
                        yield EventEnvelope.model_validate_json(msg["data"])
                    except Exception as exc:
                        log.warning("sse.live_parse_failed", error=str(exc))

                if time.monotonic() - last_heartbeat > _HEARTBEAT_SECONDS:
                    yield EventEnvelope.heartbeat()
                    last_heartbeat = time.monotonic()
        except asyncio.CancelledError:
            raise
        finally:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.aclose()
            except Exception:
                pass


sse_broker = SSEBroker()
