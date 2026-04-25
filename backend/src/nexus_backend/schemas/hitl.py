from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# UX is strict: el usuario sólo puede aceptar la corrección sugerida por el
# validador. `keep_user_value` y `custom` quedaron deprecados en abril 2026
# para evitar que un usuario distraído promueva sus propios datos sobre OCR
# de alta confianza. El historial original sigue accesible vía
# `expense_events` (evento `created` con `details.user_reported`).
HitlDecision = Literal["accept_ocr"]
HitlStatus = Literal["pending", "resolved", "expired"]


class HitlResolution(BaseModel):
    decision: HitlDecision = "accept_ocr"
    # Siempre vacío bajo la nueva UX. Lo dejamos en el schema para permitir
    # forward-compat si más adelante reintroducimos overrides manuales.
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
