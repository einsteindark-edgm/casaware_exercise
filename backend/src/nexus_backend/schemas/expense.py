from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

ExpenseStatus = Literal["pending", "processing", "hitl_required", "approved", "rejected"]


class ExpenseCreate(BaseModel):
    amount: float = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    date: date
    vendor: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=80)


class ExpenseCreated(BaseModel):
    expense_id: str
    workflow_id: str
    status: Literal["processing"] = "processing"


class ExpenseRead(BaseModel):
    expense_id: str
    tenant_id: str
    user_id: str
    amount: float
    currency: str
    date: datetime
    vendor: str
    category: str
    receipt_id: str
    workflow_id: str
    status: ExpenseStatus
    created_at: datetime
    updated_at: datetime


class ExpenseListResponse(BaseModel):
    items: list[ExpenseRead]
    next_cursor: str | None = None


class Actor(BaseModel):
    type: Literal["user", "system", "agent"]
    id: str


class ExpenseEventRead(BaseModel):
    event_id: str
    expense_id: str
    tenant_id: str
    event_type: str
    actor: Actor
    details: dict[str, Any] = Field(default_factory=dict)
    workflow_id: str | None = None
    created_at: datetime
