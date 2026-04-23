from __future__ import annotations

from fastapi import APIRouter

from nexus_backend.api.v1 import chat, dev, events, expenses, hitl, workflows
from nexus_backend.config import settings

api_v1 = APIRouter(prefix="/api/v1")
api_v1.include_router(expenses.router)
api_v1.include_router(workflows.router)
api_v1.include_router(hitl.router)
api_v1.include_router(events.router)
api_v1.include_router(chat.router)
if settings.auth_mode == "dev":
    api_v1.include_router(dev.router)
