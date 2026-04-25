from __future__ import annotations

import json
from datetime import UTC, datetime
from datetime import time as dtime
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from nexus_backend.auth.dependencies import get_current_user
from nexus_backend.auth.models import CognitoUser
from nexus_backend.config import settings
from nexus_backend.errors import ResourceNotFound, ValidationFailed
from nexus_backend.observability.logging import bind_request_context, get_logger
from nexus_backend.observability.metrics import workflow_starts
from nexus_backend.observability.otel import current_trace_id_hex, inject_traceparent
from nexus_backend.observability.xray import current_trace_header
from nexus_backend.schemas.events import EventEnvelope
from nexus_backend.schemas.expense import (
    ExpenseCreate,
    ExpenseCreated,
    ExpenseEventRead,
    ExpenseListResponse,
    ExpenseRead,
)
from nexus_backend.services.mongodb import mongo
from nexus_backend.services.s3 import s3_service
from nexus_backend.services.sse_broker import sse_broker, user_channel, workflow_channel
from nexus_backend.services.temporal_client import temporal_service
from nexus_backend.ulid_ids import new_event_id, new_expense_id, new_receipt_id

log = get_logger(__name__)
router = APIRouter(prefix="/expenses", tags=["expenses"])

_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
_ALLOWED_MIME = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "application/pdf": "pdf",
}


def _sniff_mime(data: bytes) -> str | None:
    try:
        import magic

        return magic.from_buffer(data, mime=True)
    except Exception as exc:  # libmagic missing — don't fail the whole app
        log.warning("magic.sniff_failed", error=str(exc))
        return None


async def _read_upload(upload: UploadFile) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_UPLOAD_BYTES:
            raise ValidationFailed("file exceeds 10 MB limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _isoformat(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@router.post("", response_model=ExpenseCreated, status_code=202)
async def create_expense(
    request: Request,  # noqa: ARG001
    file: UploadFile = File(..., description="Receipt image or PDF (max 10 MB)"),
    expense_json: str = Form(..., description="JSON with amount, currency, date, vendor, category"),
    user: CognitoUser = Depends(get_current_user),
) -> ExpenseCreated:
    try:
        payload = json.loads(expense_json)
    except json.JSONDecodeError as exc:
        raise ValidationFailed(f"expense_json is not valid JSON: {exc}") from exc

    expense = ExpenseCreate.model_validate(payload)

    data = await _read_upload(file)
    declared_mime = (file.content_type or "").lower()
    sniffed = _sniff_mime(data)
    effective_mime = sniffed or declared_mime
    if effective_mime not in _ALLOWED_MIME:
        raise ValidationFailed(
            f"unsupported file type: declared={declared_mime!r} sniffed={sniffed!r}"
        )
    ext = _ALLOWED_MIME[effective_mime]

    expense_id = new_expense_id()
    receipt_id = new_receipt_id()
    workflow_id = f"expense-audit-{expense_id}"
    now = datetime.now(UTC)
    s3_key = f"tenant={user.tenant_id}/user={user.sub}/{receipt_id}.{ext}"

    bind_request_context(workflow_id=workflow_id)

    await s3_service.upload_bytes(
        bucket=settings.s3_receipts_bucket,
        key=s3_key,
        data=data,
        content_type=effective_mime,
    )

    await mongo.db.receipts.insert_one(
        {
            "receipt_id": receipt_id,
            "tenant_id": user.tenant_id,
            "user_id": user.sub,
            "expense_id": expense_id,
            "s3_bucket": settings.s3_receipts_bucket,
            "s3_key": s3_key,
            "mime_type": effective_mime,
            "size_bytes": len(data),
            "uploaded_at": now,
        }
    )

    await mongo.db.expenses.insert_one(
        {
            "expense_id": expense_id,
            "tenant_id": user.tenant_id,
            "user_id": user.sub,
            "amount": expense.amount,
            "currency": expense.currency,
            "date": datetime.combine(expense.date, dtime.min, tzinfo=UTC),
            "vendor": expense.vendor,
            "category": expense.category,
            "receipt_id": receipt_id,
            "workflow_id": workflow_id,
            "status": "pending",
            "created_at": now,
            "updated_at": now,
        }
    )

    trace_id_hex = current_trace_id_hex()
    event_metadata: dict[str, Any] = {}
    if trace_id_hex:
        event_metadata["trace_id"] = trace_id_hex

    await mongo.db.expense_events.insert_one(
        {
            "event_id": new_event_id(),
            "expense_id": expense_id,
            "tenant_id": user.tenant_id,
            "event_type": "created",
            "actor": {"type": "user", "id": user.sub},
            "details": {"user_reported": expense.model_dump(mode="json")},
            "workflow_id": workflow_id,
            "metadata": event_metadata,
            "created_at": now,
        }
    )

    memo: dict[str, Any] = {}
    trace_header = current_trace_header()
    if trace_header:
        memo["x_amzn_trace_id"] = trace_header
    if trace_id_hex:
        memo["trace_id"] = trace_id_hex

    # Phase E.2 — propagate W3C tracecontext + X-Ray header to the worker so the
    # ExpenseAudit workflow + activities show up under the same trace as the HTTP
    # request in ServiceLens.
    propagation_carrier: dict[str, str] = {}
    inject_traceparent(propagation_carrier)
    propagation_headers: dict[str, bytes] = {
        k: v.encode("utf-8") for k, v in propagation_carrier.items()
    }

    await temporal_service.start_workflow(
        "ExpenseAuditWorkflow",
        args=[
            {
                "expense_id": expense_id,
                "tenant_id": user.tenant_id,
                "user_id": user.sub,
                "receipt_s3_key": s3_key,
                "user_reported_data": expense.model_dump(mode="json"),
            }
        ],
        workflow_id=workflow_id,
        task_queue="nexus-orchestrator-tq",
        memo=memo,
        headers=propagation_headers,
    )
    workflow_starts.labels(
        workflow_type="ExpenseAuditWorkflow", tenant_id=user.tenant_id
    ).inc()

    started_event = EventEnvelope(
        event_type="workflow.started",
        workflow_id=workflow_id,
        tenant_id=user.tenant_id,
        user_id=user.sub,
        expense_id=expense_id,
        payload={"expense_id": expense_id, "status": "running"},
    )
    await sse_broker.publish_many(
        [
            user_channel(user.tenant_id, user.sub),
            workflow_channel(user.tenant_id, workflow_id),
        ],
        started_event,
    )

    return ExpenseCreated(expense_id=expense_id, workflow_id=workflow_id)


@router.get("", response_model=ExpenseListResponse)
async def list_expenses(
    status: str | None = Query(default=None),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    user: CognitoUser = Depends(get_current_user),
) -> ExpenseListResponse:
    query: dict[str, Any] = {"tenant_id": user.tenant_id}
    if status:
        query["status"] = status
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if cursor:
        query["expense_id"] = {"$lt": cursor}

    docs = (
        await mongo.db.expenses.find(query, {"_id": 0})
        .sort("expense_id", -1)
        .limit(limit + 1)
        .to_list(length=limit + 1)
    )
    has_more = len(docs) > limit
    docs = docs[:limit]
    items = [ExpenseRead.model_validate(d) for d in docs]
    next_cursor = items[-1].expense_id if has_more and items else None
    return ExpenseListResponse(items=items, next_cursor=next_cursor)


@router.get("/{expense_id}", response_model=ExpenseRead)
async def get_expense(
    expense_id: str,
    user: CognitoUser = Depends(get_current_user),
) -> ExpenseRead:
    doc = await mongo.db.expenses.find_one(
        {"expense_id": expense_id, "tenant_id": user.tenant_id},
        {"_id": 0},
    )
    if not doc:
        raise ResourceNotFound(f"expense {expense_id} not found")
    return ExpenseRead.model_validate(doc)


@router.get("/{expense_id}/history", response_model=list[ExpenseEventRead])
async def get_expense_history(
    expense_id: str,
    user: CognitoUser = Depends(get_current_user),
) -> list[ExpenseEventRead]:
    expense = await mongo.db.expenses.find_one(
        {"expense_id": expense_id, "tenant_id": user.tenant_id},
        {"_id": 0, "expense_id": 1, "status": 1},
    )
    if not expense:
        raise ResourceNotFound(f"expense {expense_id} not found")

    cache_key = f"nexus:cache:expense_events:{user.tenant_id}:{expense_id}"
    cacheable = (
        settings.expense_history_cache and expense.get("status") in {"approved", "rejected"}
    )
    if cacheable:
        from nexus_backend.services.redis_client import redis_pool

        cached = await redis_pool.client.get(cache_key)
        if cached:
            return [ExpenseEventRead.model_validate_json(x) for x in json.loads(cached)]

    cursor = (
        mongo.db.expense_events.find(
            {"expense_id": expense_id, "tenant_id": user.tenant_id},
            {"_id": 0},
        )
        .sort("created_at", 1)
        .limit(200)
    )
    events = await cursor.to_list(length=200)
    result = [ExpenseEventRead.model_validate(e) for e in events]

    if cacheable:
        from nexus_backend.services.redis_client import redis_pool

        serialised = json.dumps([r.model_dump_json() for r in result])
        await redis_pool.client.set(cache_key, serialised, ex=60)

    return result


@router.get("/{expense_id}/receipt")
async def get_expense_receipt(
    expense_id: str,
    user: CognitoUser = Depends(get_current_user),
) -> StreamingResponse:
    expense = await mongo.db.expenses.find_one(
        {"expense_id": expense_id, "tenant_id": user.tenant_id},
        {"_id": 0, "receipt_id": 1},
    )
    if not expense:
        raise ResourceNotFound(f"expense {expense_id} not found")

    receipt = await mongo.db.receipts.find_one(
        {"receipt_id": expense["receipt_id"], "tenant_id": user.tenant_id},
        {"_id": 0, "s3_bucket": 1, "s3_key": 1, "mime_type": 1},
    )
    if not receipt:
        raise ResourceNotFound(f"receipt for expense {expense_id} not found")

    async with s3_service._client() as s3:  # noqa: SLF001
        obj = await s3.get_object(Bucket=receipt["s3_bucket"], Key=receipt["s3_key"])
        data = await obj["Body"].read()

    async def body_iter():
        yield data

    return StreamingResponse(
        body_iter(),
        media_type=receipt.get("mime_type") or "application/octet-stream",
        headers={"Cache-Control": "private, max-age=60"},
    )


__all__ = ["router"]

# Silence unused import warnings from helpers imported for JSON serialisation.
_ = JSONResponse, _isoformat
