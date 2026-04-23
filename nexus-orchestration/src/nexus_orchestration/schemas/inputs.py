"""Workflow input dataclasses.

Dataclasses (not Pydantic) because:
  * temporalio serialises stdlib dataclasses natively.
  * The backend sends plain dicts; dataclasses are constructed from them by
    temporalio's default converter without extra configuration.

Shapes match what the backend sends today — see:
  backend/src/nexus_backend/api/v1/expenses.py for ExpenseAuditInput
  backend/src/nexus_backend/api/v1/chat.py for RAGQueryInput
  backend/src/nexus_backend/api/v1/hitl.py for HitlResponse
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExpenseAuditInput:
    expense_id: str
    tenant_id: str
    user_id: str
    receipt_s3_key: str
    # Free-form dict with what the user reported from the frontend form
    # (amount, currency, date, vendor, category).
    user_reported_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class RAGQueryInput:
    session_id: str
    turn: int
    tenant_id: str
    user_id: str
    message: str


@dataclass
class HitlResponse:
    decision: str  # "accept_ocr" | "keep_user_value" | "custom"
    resolved_fields: dict[str, Any] = field(default_factory=dict)
    user_id: str = ""
    timestamp: str = ""


@dataclass
class CancelAudit:
    reason: str = ""
    user_id: str = ""
