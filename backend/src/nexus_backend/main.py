from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from ulid import ULID

from nexus_backend.api.health import router as health_router
from nexus_backend.api.v1.router import api_v1
from nexus_backend.auth.cognito import prime_jwks
from nexus_backend.config import settings
from nexus_backend.errors import register_exception_handlers
from nexus_backend.observability.logging import (
    configure_logging,
    get_logger,
    reset_request_context,
    set_request_id,
)
from nexus_backend.observability.metrics import install_metrics
from nexus_backend.observability.otel import install_otel
from nexus_backend.observability.xray import install_xray
from nexus_backend.services.mongodb import mongo
from nexus_backend.services.redis_client import redis_pool
from nexus_backend.services.s3 import s3_service
from nexus_backend.services.temporal_client import temporal_service

log = get_logger(__name__)


def _rate_limit_key(request: Request) -> str:
    """Rate limit by authenticated user sub when available, else by client IP."""
    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return f"user:{hash(auth)}"
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(key_func=_rate_limit_key, default_limits=[settings.rate_limit_default])


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        request_id = request.headers.get("x-request-id") or str(ULID())
        set_request_id(request_id)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("request.unhandled_error", path=request.url.path)
            raise
        else:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            response.headers["X-Request-ID"] = request_id
            # Log at INFO; structlog processors redact Authorization automatically since
            # we never pass it into the log context.
            log.info(
                "request.completed",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                elapsed_ms=elapsed_ms,
            )
            return response
        finally:
            reset_request_context()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:  # noqa: ARG001
    configure_logging()
    log.info("startup.begin", env=settings.env, auth_mode=settings.auth_mode)

    await mongo.connect()
    await redis_pool.connect()
    await temporal_service.connect()
    # Resolve AWS task-role credentials once now so the first user request
    # doesn't time out on the ECS metadata endpoint (169.254.170.2).
    await s3_service.prime()
    try:
        await prime_jwks()
    except Exception as exc:
        log.warning("jwks.prime_failed", error=str(exc))

    log.info("startup.complete")
    try:
        yield
    finally:
        log.info("shutdown.begin")
        await temporal_service.close()
        await redis_pool.close()
        await mongo.close()
        log.info("shutdown.complete")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Nexus Backend",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.state.limiter = limiter

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "Last-Event-ID"],
    )
    app.add_middleware(RequestContextMiddleware)

    # Order matters: otel/xray before metrics so spans wrap metric reporting.
    install_otel(app)
    install_xray(app)
    install_metrics(app)

    register_exception_handlers(app)

    async def _rate_limit_handler(
        request: Request,  # noqa: ARG001
        exc: RateLimitExceeded,
    ) -> Response:
        return Response(
            content='{"error":{"code":"rate_limited","message":"' + str(exc.detail) + '"}}',
            media_type="application/json",
            status_code=429,
        )

    app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)

    app.include_router(health_router)
    app.include_router(api_v1)

    return app


app = create_app()
