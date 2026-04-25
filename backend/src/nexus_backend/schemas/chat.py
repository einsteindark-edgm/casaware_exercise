from __future__ import annotations

from datetime import datetime

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


class ChatCitation(BaseModel):
    expense_id: str
    vendor: str | None = None
    amount: float | None = None
    currency: str | None = None
    date: str | None = None
    category: str | None = None
    link: str
    source: str | None = None


class ChatTurn(BaseModel):
    turn: int
    user_message: str
    assistant_message: str
    citations: list[ChatCitation] = Field(default_factory=list)
    created_at: datetime | None = None


class ChatSessionHistory(BaseModel):
    session_id: str
    tenant_id: str
    turns: list[ChatTurn]
