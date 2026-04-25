"""OpenTelemetry bootstrap (Phase E.2).

Replaces the legacy X-Ray SDK middleware with OTel SDK + OTLP exporter
pointing at the ADOT sidecar (localhost:4317). The sidecar then forwards
to AWS X-Ray + CloudWatch metrics.

Why OTel and not X-Ray SDK directly: OTel is the standard, instruments
Temporal/gRPC natively, and ADOT translates to X-Ray IDs automatically
when we pin the AwsXRayIdGenerator + AwsXRayPropagator. Same destination,
better cross-service propagation.

The whole module is a no-op when OTEL_ENABLED is unset/false so dev does
not need a collector running.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI


_installed = False


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def install_otel(app: "FastAPI") -> None:
    """Install OpenTelemetry instrumentation. Idempotent + safe when disabled."""
    global _installed
    if _installed:
        return
    if not _truthy(os.getenv("OTEL_ENABLED")):
        return

    # Imports are local so the cold path stays free of OTel deps when disabled.
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.pymongo import PymongoInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.propagate import set_global_textmap
    from opentelemetry.propagators.composite import CompositePropagator
    from opentelemetry.propagators.aws import AwsXRayPropagator
    from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    resource = Resource.create(
        {
            "service.name": os.getenv("OTEL_SERVICE_NAME", "nexus-backend"),
            "service.namespace": "nexus",
            "deployment.environment": os.getenv("ENV", "dev"),
        }
    )
    provider = TracerProvider(resource=resource, id_generator=AwsXRayIdGenerator())
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)

    # W3C tracecontext for cross-language propagation + X-Ray header for
    # ServiceLens compatibility. CompositePropagator extracts whichever is
    # present.
    set_global_textmap(CompositePropagator([TraceContextTextMapPropagator(), AwsXRayPropagator()]))

    FastAPIInstrumentor.instrument_app(app)
    BotocoreInstrumentor().instrument()
    PymongoInstrumentor().instrument()
    RedisInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()

    _installed = True


def current_trace_id_hex() -> str | None:
    """Return the current trace_id as 32-char hex string, or None if no span."""
    if not _installed:
        return None
    from opentelemetry import trace

    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None
    return format(ctx.trace_id, "032x")


def inject_traceparent(carrier: dict[str, str]) -> dict[str, str]:
    """Inject W3C traceparent + X-Ray header into a carrier dict for propagation."""
    if not _installed:
        return carrier
    from opentelemetry.propagate import inject

    inject(carrier)
    return carrier


__all__ = ["install_otel", "current_trace_id_hex", "inject_traceparent"]
