from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any

import structlog

from nexus_backend.config import settings

_request_id: ContextVar[str | None] = ContextVar("nexus_request_id", default=None)
_tenant_id: ContextVar[str | None] = ContextVar("nexus_tenant_id", default=None)
_user_id: ContextVar[str | None] = ContextVar("nexus_user_id", default=None)
_workflow_id: ContextVar[str | None] = ContextVar("nexus_workflow_id", default=None)


def _inject_context(
    logger: Any,  # noqa: ARG001
    method_name: str,  # noqa: ARG001
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    for key, var in (
        ("request_id", _request_id),
        ("tenant_id", _tenant_id),
        ("user_id", _user_id),
        ("workflow_id", _workflow_id),
    ):
        value = var.get()
        if value and key not in event_dict:
            event_dict[key] = value
    # Phase E.2 — attach trace_id/span_id from current OTel span if present.
    # Lets CloudWatch Logs Insights filter by trace_id alongside ServiceLens.
    if "trace_id" not in event_dict:
        try:
            from opentelemetry import trace as _otel_trace

            ctx = _otel_trace.get_current_span().get_span_context()
            if ctx.is_valid:
                event_dict["trace_id"] = format(ctx.trace_id, "032x")
                event_dict["span_id"] = format(ctx.span_id, "016x")
        except Exception:
            pass
    return event_dict


def configure_logging() -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(message)s")

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _inject_context,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


def set_request_id(value: str) -> None:
    _request_id.set(value)


def get_request_id() -> str | None:
    return _request_id.get()


def bind_request_context(
    *,
    tenant_id: str | None = None,
    user_id: str | None = None,
    workflow_id: str | None = None,
) -> None:
    if tenant_id is not None:
        _tenant_id.set(tenant_id)
    if user_id is not None:
        _user_id.set(user_id)
    if workflow_id is not None:
        _workflow_id.set(workflow_id)


def reset_request_context() -> None:
    _request_id.set(None)
    _tenant_id.set(None)
    _user_id.set(None)
    _workflow_id.set(None)
