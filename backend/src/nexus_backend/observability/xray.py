from __future__ import annotations

from fastapi import FastAPI

from nexus_backend.config import settings


def install_xray(app: FastAPI) -> None:
    """Install the X-Ray middleware when XRAY_ENABLED=true. No-op otherwise."""
    if not settings.xray_enabled:
        return

    from aws_xray_sdk.core import xray_recorder
    from aws_xray_sdk.ext.fastapi.middleware import XRayMiddleware

    xray_recorder.configure(service=f"nexus-backend-{settings.env}")
    app.add_middleware(XRayMiddleware, recorder=xray_recorder)


def current_trace_header() -> str | None:
    """Return the current X-Ray trace header, or None if disabled/unavailable."""
    if not settings.xray_enabled:
        return None
    try:
        from aws_xray_sdk.core import xray_recorder

        seg = xray_recorder.current_segment()
        if not seg:
            return None
        return f"Root={seg.trace_id};Parent={seg.id};Sampled={'1' if seg.sampled else '0'}"
    except Exception:
        return None
