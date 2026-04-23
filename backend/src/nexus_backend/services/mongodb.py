from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from nexus_backend.config import settings


class Mongo:
    def __init__(self) -> None:
        self._client: AsyncIOMotorClient | None = None
        self._db: AsyncIOMotorDatabase | None = None

    async def connect(self) -> None:
        self._client = AsyncIOMotorClient(settings.mongodb_uri, tz_aware=True)
        self._db = self._client[settings.mongodb_db]
        await self._ensure_indexes()

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()
        self._client = None
        self._db = None

    @property
    def db(self) -> AsyncIOMotorDatabase:
        if self._db is None:
            raise RuntimeError("mongo not connected")
        return self._db

    async def ping(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.admin.command("ping")
            return True
        except Exception:
            return False

    async def _ensure_indexes(self) -> None:
        db = self.db
        await db.expenses.create_index([("tenant_id", 1), ("expense_id", 1)], unique=True)
        await db.expenses.create_index([("tenant_id", 1), ("status", 1), ("created_at", -1)])
        await db.expenses.create_index([("workflow_id", 1)], unique=True, sparse=True)
        await db.receipts.create_index([("receipt_id", 1)], unique=True)
        await db.receipts.create_index([("tenant_id", 1), ("user_id", 1)])
        await db.hitl_tasks.create_index([("task_id", 1)], unique=True)
        await db.hitl_tasks.create_index([("tenant_id", 1), ("status", 1)])
        await db.expense_events.create_index(
            [("tenant_id", 1), ("expense_id", 1), ("created_at", 1)]
        )
        await db.chat_sessions.create_index([("session_id", 1)], unique=True)
        await db.chat_sessions.create_index([("tenant_id", 1), ("user_id", 1)])


mongo = Mongo()
