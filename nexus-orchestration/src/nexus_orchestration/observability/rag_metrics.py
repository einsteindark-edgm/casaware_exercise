"""Observability hooks for the hybrid RAG tools.

Every tool call (structured SQL or semantic vector) goes through
`record_tool_call`, which emits a structured log entry and (if
`prometheus_client` is installed) increments a Prometheus counter. Callers
wrap their activity body with `tool_call_span` to capture latency + outcome
without duplicating try/except in every activity.

Why here and not inside each activity: observability should be independent of
the underlying provider (Databricks SQL / VS). Keeping it in one helper means
new tools get the same telemetry for free.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any

from nexus_orchestration.observability.logging import get_logger

log = get_logger(__name__)

try:
    from prometheus_client import Counter  # type: ignore[import-not-found]

    _tool_calls_total = Counter(
        "rag_tool_calls_total",
        "Hybrid-RAG tool invocations",
        labelnames=("tool", "outcome"),
    )
except Exception:  # pragma: no cover — prometheus is optional in dev
    _tool_calls_total = None

try:
    from opentelemetry import trace as _otel_trace  # type: ignore[import-not-found]

    _tracer = _otel_trace.get_tracer("nexus.rag.tools")
except Exception:  # pragma: no cover — OTel is optional in dev
    _tracer = None


def record_tool_call(
    *,
    tool: str,
    outcome: str,
    latency_ms: float,
    row_count: int = 0,
    workflow_id: str | None = None,
    error: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "tool": tool,
        "outcome": outcome,
        "latency_ms": round(latency_ms, 2),
        "row_count": row_count,
    }
    if workflow_id:
        payload["workflow_id"] = workflow_id
    if error:
        payload["error"] = error
    if extra:
        payload.update(extra)
    log.info("rag.tool_call", **payload)
    if _tool_calls_total is not None:
        try:
            _tool_calls_total.labels(tool=tool, outcome=outcome).inc()
        except Exception:  # pragma: no cover — never fail a request on metrics
            pass


@contextmanager
def tool_call_span(
    *,
    tool: str,
    workflow_id: str | None = None,
    extra: dict[str, Any] | None = None,
):
    """Wrap an activity body to emit latency + outcome automatically.

    Yields a mutable `info` dict; callers should set `info["row_count"]` (and
    optionally `info["outcome"] = "empty"`) when the activity succeeds. On
    exception the span logs `outcome="error"` and re-raises.

    Phase E.3: also opens an OpenTelemetry span so the tool call shows up as
    a child of the workflow/activity span in ServiceLens.
    """
    start = time.perf_counter()
    info: dict[str, Any] = {"row_count": 0, "outcome": "ok"}

    if _tracer is not None:
        otel_cm = _tracer.start_as_current_span(f"rag.tool.{tool}")
    else:
        from contextlib import nullcontext

        otel_cm = nullcontext()

    with otel_cm as span:
        if span is not None and hasattr(span, "set_attribute"):
            span.set_attribute("rag.tool", tool)
            if workflow_id:
                span.set_attribute("temporal.workflow_id", workflow_id)
        try:
            yield info
        except Exception as exc:
            latency_ms = (time.perf_counter() - start) * 1000
            if span is not None and hasattr(span, "record_exception"):
                span.record_exception(exc)
                span.set_attribute("rag.outcome", "error")
            record_tool_call(
                tool=tool,
                outcome="error",
                latency_ms=latency_ms,
                row_count=0,
                workflow_id=workflow_id,
                error=type(exc).__name__ + ": " + str(exc),
                extra=extra,
            )
            raise
        else:
            latency_ms = (time.perf_counter() - start) * 1000
            outcome = info.get("outcome") or "ok"
            if outcome == "ok" and not info.get("row_count"):
                outcome = "empty"
            if span is not None and hasattr(span, "set_attribute"):
                span.set_attribute("rag.outcome", outcome)
                span.set_attribute("rag.row_count", int(info.get("row_count", 0)))
            record_tool_call(
                tool=tool,
                outcome=outcome,
                latency_ms=latency_ms,
                row_count=int(info.get("row_count", 0)),
                workflow_id=workflow_id,
                extra=extra,
            )
