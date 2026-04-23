from __future__ import annotations

from fastapi import FastAPI
from prometheus_client import Counter, Gauge
from prometheus_fastapi_instrumentator import Instrumentator

sse_connections_active = Gauge(
    "nexus_sse_connections_active",
    "Number of open SSE connections",
    labelnames=["tenant_id"],
)
sse_events_published = Counter(
    "nexus_sse_events_published_total",
    "Total events published to Redis by the backend",
    labelnames=["event_type"],
)
workflow_starts = Counter(
    "nexus_workflow_starts_total",
    "Total Temporal workflows started",
    labelnames=["workflow_type", "tenant_id"],
)
hitl_resolutions = Counter(
    "nexus_hitl_resolutions_total",
    "HITL task resolutions by decision",
    labelnames=["decision"],
)


def install_metrics(app: FastAPI) -> None:
    instrumentator = Instrumentator(
        should_group_status_codes=True,
        should_ignore_untemplated=True,
        excluded_handlers=["/healthz", "/readyz", "/metrics"],
    )
    instrumentator.instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
