from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from nexus_backend.observability.logging import get_request_id


class NexusError(Exception):
    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str = "", *, extra: dict | None = None) -> None:
        super().__init__(message)
        self.message = message or self.code
        self.extra = extra or {}


class NotAuthenticated(NexusError):
    status_code = 401
    code = "not_authenticated"


class NotAuthorized(NexusError):
    status_code = 403
    code = "not_authorized"


class ResourceNotFound(NexusError):
    status_code = 404
    code = "not_found"


class ValidationFailed(NexusError):
    status_code = 422
    code = "validation_failed"


class TemporalUnavailable(NexusError):
    status_code = 503
    code = "temporal_unavailable"


class UpstreamUnavailable(NexusError):
    status_code = 503
    code = "upstream_unavailable"


def _envelope(code: str, message: str, status: int, extra: dict | None = None) -> JSONResponse:
    body: dict = {
        "error": {
            "code": code,
            "message": message,
            "request_id": get_request_id(),
        }
    }
    if extra:
        body["error"]["details"] = extra
    return JSONResponse(status_code=status, content=body)


async def _handle_nexus(request: Request, exc: NexusError) -> JSONResponse:  # noqa: ARG001
    return _envelope(exc.code, exc.message, exc.status_code, exc.extra)


async def _handle_validation(
    request: Request,  # noqa: ARG001
    exc: RequestValidationError,
) -> JSONResponse:
    return _envelope(
        "validation_failed",
        "request validation failed",
        422,
        {"errors": exc.errors()},
    )


async def _handle_unhandled(request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG001
    return _envelope("internal_error", str(exc) or "internal error", 500)


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(NexusError, _handle_nexus)
    app.add_exception_handler(RequestValidationError, _handle_validation)
    app.add_exception_handler(Exception, _handle_unhandled)
