from __future__ import annotations

import redis.asyncio as redis

from nexus_backend.config import settings


class RedisPool:
    def __init__(self) -> None:
        self._client: redis.Redis | None = None

    async def connect(self) -> None:
        kwargs: dict = {
            "encoding": "utf-8",
            "decode_responses": True,
            "health_check_interval": 30,
        }
        if settings.redis_tls:
            kwargs["ssl"] = True
        self._client = redis.from_url(settings.redis_url, **kwargs)
        await self._client.ping()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        self._client = None

    @property
    def client(self) -> redis.Redis:
        if self._client is None:
            raise RuntimeError("redis not connected")
        return self._client

    async def ping(self) -> bool:
        if self._client is None:
            return False
        try:
            return bool(await self._client.ping())
        except Exception:
            return False


redis_pool = RedisPool()
