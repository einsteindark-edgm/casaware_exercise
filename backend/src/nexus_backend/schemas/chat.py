from __future__ import annotations

from pydantic import BaseModel, Field


class ChatStartRequest(BaseModel):
    message: str = Field(min_length=1)
    session_id: str | None = None


class ChatStartResponse(BaseModel):
    session_id: str
    workflow_id: str
    turn: int


class ChatStreamChunk(BaseModel):
    token: str
    session_id: str
