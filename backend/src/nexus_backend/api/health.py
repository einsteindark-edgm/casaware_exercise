from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from nexus_backend.auth.cognito import is_jwks_ready
from nexus_backend.services.mongodb import mongo
from nexus_backend.services.redis_client import redis_pool
from nexus_backend.services.temporal_client import temporal_service

router = APIRouter(tags=["health"])


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
async def readyz() -> JSONResponse:
    checks = {
        "mongo": await mongo.ping(),
        "redis": await redis_pool.ping(),
        "temporal": await temporal_service.ping(),
        "cognito_jwks": is_jwks_ready(),
    }
    ok = all(checks.values())
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ready" if ok else "degraded", "checks": checks},
    )
