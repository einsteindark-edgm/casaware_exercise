from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Response

from nexus_backend.auth.dependencies import get_current_user
from nexus_backend.auth.models import CognitoUser
from nexus_backend.errors import NotAuthorized, ResourceNotFound
from nexus_backend.observability.logging import bind_request_context
from nexus_backend.observability.metrics import hitl_resolutions
from nexus_backend.schemas.events import EventEnvelope
from nexus_backend.schemas.hitl import HitlResolution, HitlTaskRead
from nexus_backend.services.mongodb import mongo
from nexus_backend.services.sse_broker import sse_broker, user_channel, workflow_channel
from nexus_backend.services.temporal_client import temporal_service

router = APIRouter(prefix="/hitl", tags=["hitl"])


@router.get("", response_model=list[HitlTaskRead])
async def list_hitl_tasks(
    workflow_id: str | None = Query(default=None),
    expense_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    user: CognitoUser = Depends(get_current_user),
) -> list[HitlTaskRead]:
    query: dict[str, Any] = {"tenant_id": user.tenant_id}
    if workflow_id:
        query["workflow_id"] = workflow_id
    if expense_id:
        query["expense_id"] = expense_id
    if status:
        query["status"] = status
    docs = (
        await mongo.db.hitl_tasks.find(query, {"_id": 0})
        .sort("created_at", -1)
        .limit(50)
        .to_list(length=50)
    )
    return [HitlTaskRead.model_validate(d) for d in docs]


@router.get("/{task_id}", response_model=HitlTaskRead)
async def get_hitl_task(
    task_id: str,
    user: CognitoUser = Depends(get_current_user),
) -> HitlTaskRead:
    task = await mongo.db.hitl_tasks.find_one({"task_id": task_id}, {"_id": 0})
    if task is None:
        raise ResourceNotFound(f"hitl task {task_id} not found")
    if task.get("tenant_id") != user.tenant_id:
        raise NotAuthorized("hitl task belongs to a different tenant")
    return HitlTaskRead.model_validate(task)


@router.post("/{task_id}/resolve", status_code=204)
async def resolve_hitl(
    task_id: str,
    body: HitlResolution,
    user: CognitoUser = Depends(get_current_user),
) -> Response:
    task = await mongo.db.hitl_tasks.find_one({"task_id": task_id}, {"_id": 0})
    if task is None:
        raise ResourceNotFound(f"hitl task {task_id} not found")
    if task.get("tenant_id") != user.tenant_id:
        raise NotAuthorized("hitl task belongs to a different tenant")
    if task.get("status") != "pending":
        raise NotAuthorized(f"hitl task already {task.get('status')}")

    workflow_id = task["workflow_id"]
    bind_request_context(workflow_id=workflow_id)

    now = datetime.now(UTC)
    await temporal_service.signal(
        workflow_id,
        "hitl_response",
        {
            "resolved_fields": body.resolved_fields,
            "decision": body.decision,
            "user_id": user.sub,
            "timestamp": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        },
    )

    await mongo.db.hitl_tasks.update_one(
        {"task_id": task_id},
        {
            "$set": {
                "status": "resolved",
                "resolved_at": now,
                "decision": body.decision,
                "resolved_fields": body.resolved_fields,
                "resolved_by": user.sub,
            }
        },
    )

    await mongo.db.expense_events.insert_one(
        {
            "event_id": EventEnvelope(
                event_type="workflow.hitl_resolved",
                tenant_id=user.tenant_id,
            ).event_id,
            "expense_id": task.get("expense_id"),
            "tenant_id": user.tenant_id,
            "event_type": "hitl_resolved",
            "actor": {"type": "user", "id": user.sub},
            "details": {
                "decision": body.decision,
                "resolved_fields": body.resolved_fields,
                "hitl_task_id": task_id,
            },
            "workflow_id": workflow_id,
            "created_at": now,
        }
    )

    hitl_resolutions.labels(decision=body.decision).inc()

    event = EventEnvelope(
        event_type="workflow.hitl_resolved",
        workflow_id=workflow_id,
        tenant_id=user.tenant_id,
        user_id=user.sub,
        expense_id=task.get("expense_id"),
        payload={"hitl_task_id": task_id, "decision": body.decision},
    )
    await sse_broker.publish_many(
        [
            user_channel(user.tenant_id, user.sub),
            workflow_channel(user.tenant_id, workflow_id),
        ],
        event,
    )

    return Response(status_code=204)
