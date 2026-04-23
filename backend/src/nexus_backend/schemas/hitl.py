from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

HitlDecision = Literal["accept_ocr", "keep_user_value", "custom"]
HitlStatus = Literal["pending", "resolved", "expired"]


class HitlResolution(BaseModel):
    decision: HitlDecision
    resolved_fields: dict[str, Any] = Field(default_factory=dict)


class FieldConflict(BaseModel):
    field: str
    user_value: Any = None
    ocr_value: Any = None
    confidence: float | None = None


class HitlTaskRead(BaseModel):
    task_id: str
    expense_id: str | None = None
    workflow_id: str
    tenant_id: str
    status: HitlStatus
    fields_in_conflict: list[FieldConflict] = Field(default_factory=list)
    created_at: datetime
    resolved_at: datetime | None = None
