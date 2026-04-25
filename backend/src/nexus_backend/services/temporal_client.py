from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from nexus_backend.config import settings
from nexus_backend.errors import ResourceNotFound, TemporalUnavailable
from nexus_backend.observability.logging import get_logger

log = get_logger(__name__)


class TemporalBackend(ABC):
    """Abstract Temporal backend so the API can swap real vs fake via config."""

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def ping(self) -> bool: ...

    @abstractmethod
    async def start_workflow(
        self,
        workflow_type: str,
        *,
        args: list[Any],
        workflow_id: str,
        task_queue: str,
        memo: dict[str, Any] | None = None,
    ) -> str: ...

    @abstractmethod
    async def signal(self, workflow_id: str, signal_name: str, payload: Any) -> None: ...

    @abstractmethod
    async def query(self, workflow_id: str, query_name: str) -> Any: ...


class RealTemporalBackend(TemporalBackend):
    def __init__(self) -> None:
        self._client: Any | None = None

    async def connect(self) -> None:
        from temporalio.client import Client

        # Phase E.3 — register the OTel TracingInterceptor so the FastAPI →
        # Temporal hop carries traceparent + X-Ray headers automatically.
        # The worker's main_worker.py registers the same interceptor on its
        # client; together they keep the trace continuous in ServiceLens.
        interceptors: list[Any] = []
        try:
            from temporalio.contrib.opentelemetry import TracingInterceptor

            interceptors.append(TracingInterceptor())
        except Exception as exc:  # pragma: no cover — OTel deps optional
            log.warning("temporal.tracing_interceptor_unavailable", error=str(exc))

        self._client = await Client.connect(
            settings.temporal_host,
            namespace=settings.temporal_namespace,
            interceptors=interceptors,
        )

    async def close(self) -> None:
        self._client = None  # temporalio's Client has no explicit close

    async def ping(self) -> bool:
        if self._client is None:
            return False
        try:
            # Any lightweight RPC. list_workflows has pagination limits we can cap.
            await self._client.service_client.workflow_service.get_system_info({})
            return True
        except Exception:
            return False

    async def start_workflow(
        self,
        workflow_type: str,
        *,
        args: list[Any],
        workflow_id: str,
        task_queue: str,
        memo: dict[str, Any] | None = None,
    ) -> str:
        from temporalio.common import WorkflowIDReusePolicy
        from temporalio.service import RPCError

        if self._client is None:
            raise TemporalUnavailable("temporal client not connected")

        # OTel context propagation is implicit: the TracingInterceptor wired
        # in connect() reads the current OTel context and attaches it to the
        # outbound StartWorkflowExecution command.
        try:
            await self._client.start_workflow(
                workflow_type,
                args=args,
                id=workflow_id,
                task_queue=task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                memo=memo or {},
            )
        except RPCError as exc:
            raise TemporalUnavailable(f"start_workflow failed: {exc}") from exc
        return workflow_id

    async def signal(self, workflow_id: str, signal_name: str, payload: Any) -> None:
        from temporalio.service import RPCError

        if self._client is None:
            raise TemporalUnavailable("temporal client not connected")
        try:
            handle = self._client.get_workflow_handle(workflow_id)
            await handle.signal(signal_name, payload)
        except RPCError as exc:
            if "not found" in str(exc).lower():
                raise ResourceNotFound(f"workflow {workflow_id} not found") from exc
            raise TemporalUnavailable(f"signal failed: {exc}") from exc

    async def query(self, workflow_id: str, query_name: str) -> Any:
        from temporalio.service import RPCError

        if self._client is None:
            raise TemporalUnavailable("temporal client not connected")
        try:
            handle = self._client.get_workflow_handle(workflow_id)
            return await handle.query(query_name)
        except RPCError as exc:
            if "not found" in str(exc).lower():
                raise ResourceNotFound(f"workflow {workflow_id} not found") from exc
            raise TemporalUnavailable(f"query failed: {exc}") from exc


class _TemporalProxy(TemporalBackend):
    """Lazy proxy that picks the concrete backend based on ``settings.temporal_mode``."""

    def __init__(self) -> None:
        self._backend: TemporalBackend | None = None

    async def connect(self) -> None:
        if settings.temporal_mode == "fake":
            from tests.fakes.fake_temporal import FakeTemporalBackend

            self._backend = FakeTemporalBackend()
        else:
            self._backend = RealTemporalBackend()
        try:
            await self._backend.connect()
        except Exception as exc:
            # Degrade gracefully in dev: log and continue with a disconnected backend.
            # ping() will return False so /readyz reflects the real state.
            log.warning("temporal.connect_failed", error=str(exc), mode=settings.temporal_mode)

    async def close(self) -> None:
        if self._backend is not None:
            await self._backend.close()
        self._backend = None

    async def ping(self) -> bool:
        return bool(self._backend) and await self._backend.ping()

    async def start_workflow(self, *args: Any, **kwargs: Any) -> str:
        if self._backend is None:
            raise TemporalUnavailable("temporal backend not initialised")
        # Be tolerant of legacy callers still passing `headers=` from older
        # branches: silently drop it (propagation is interceptor-driven now).
        kwargs.pop("headers", None)
        return await self._backend.start_workflow(*args, **kwargs)

    async def signal(self, *args: Any, **kwargs: Any) -> None:
        if self._backend is None:
            raise TemporalUnavailable("temporal backend not initialised")
        await self._backend.signal(*args, **kwargs)

    async def query(self, *args: Any, **kwargs: Any) -> Any:
        if self._backend is None:
            raise TemporalUnavailable("temporal backend not initialised")
        return await self._backend.query(*args, **kwargs)


temporal_service: TemporalBackend = _TemporalProxy()
