"""OpenTelemetry bootstrap for Temporal worker (Phase E.3).

Mirrors backend/observability/otel.py but registers a TemporalIO tracing
interceptor so every workflow + activity gets a child span linked to the
trace started by the FastAPI request.

No-op when OTEL_ENABLED is unset/false (dev runs on stubs).
"""
from __future__ import annotations

import os

_installed = False


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def install_otel() -> None:
    """Install OpenTelemetry SDK + exporters. Idempotent."""
    global _installed
    if _installed:
        return
    if not _truthy(os.getenv("OTEL_ENABLED")):
        return

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
    from opentelemetry.instrumentation.pymongo import PymongoInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.propagate import set_global_textmap
    from opentelemetry.propagators.aws import AwsXRayPropagator
    from opentelemetry.propagators.composite import CompositePropagator
    from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    resource = Resource.create(
        {
            "service.name": os.getenv("OTEL_SERVICE_NAME", "nexus-worker"),
            "service.namespace": "nexus",
            "deployment.environment": os.getenv("ENV", "dev"),
        }
    )
    provider = TracerProvider(resource=resource, id_generator=AwsXRayIdGenerator())
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)

    set_global_textmap(
        CompositePropagator([TraceContextTextMapPropagator(), AwsXRayPropagator()])
    )

    BotocoreInstrumentor().instrument()
    PymongoInstrumentor().instrument()
    RedisInstrumentor().instrument()

    _installed = True


def temporal_tracing_interceptor():
    """Return a Temporal client interceptor that propagates OTel context.

    Returns None when OTel is disabled or the optional contrib package is not
    installed — callers should treat None as "skip interceptor wiring".
    """
    if not _installed:
        return None
    try:
        from temporalio.contrib.opentelemetry import TracingInterceptor

        return TracingInterceptor()
    except Exception:
        return None


def current_trace_id_hex() -> str | None:
    """Return the current trace_id as 32-char hex, or None."""
    if not _installed:
        return None
    from opentelemetry import trace

    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None
    return format(ctx.trace_id, "032x")


__all__ = ["install_otel", "temporal_tracing_interceptor", "current_trace_id_hex"]
