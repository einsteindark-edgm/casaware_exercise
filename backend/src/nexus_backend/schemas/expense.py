from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

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
    # Valores ORIGINALES reportados por el usuario. Nunca se sobrescriben:
    # son la fuente de verdad histórica para el panel "antes vs después".
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
    # Valores FINALES post-aprobación. NULL hasta que el workflow llega al
    # paso `update_expense_to_approved`. Si un campo no estuvo en conflicto,
    # es igual al original.
    final_amount: float | None = None
    final_vendor: str | None = None
    final_date: datetime | None = None
    final_currency: str | None = None
    # Mapa por campo → "user" | "ocr" | "hitl_custom". Sirve a la UI para
    # decidir qué fuente badgear y qué fila mostrar como "corregida".
    source_per_field: dict[str, str] | None = None
    approved_at: datetime | None = None
    # Computado: True si el expense pasó por HITL (se marcó al menos un
    # campo como "ocr" en source_per_field).
    had_hitl: bool = False

    @model_validator(mode="after")
    def _compute_had_hitl(self) -> "ExpenseRead":
        if self.source_per_field:
            self.had_hitl = any(v != "user" for v in self.source_per_field.values())
        return self


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
