from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

WorkflowState = Literal["running", "waiting_hitl", "completed", "failed"]


class WorkflowStatus(BaseModel):
    state: WorkflowState
    current_step: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
