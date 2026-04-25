from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

# IMPORTANT: monkey-patch the ECS container-metadata fetcher BEFORE creating
# any aioboto3/botocore session. The class attributes below are hardcoded
# in botocore (TIMEOUT_SECONDS=2, RETRY_ATTEMPTS=3) — there is no env var
# or Config option that can override them. On Fargate cold starts and at
# every ~6h credential rotation, the 169.254.170.2 endpoint can take >2s
# to respond, which surfaces as CredentialRetrievalError on whatever
# request lands during the refresh. AWS_METADATA_SERVICE_* env vars only
# apply to EC2 IMDS, NOT to ECS task-role fetch.
import aiobotocore.utils as _aiobotocore_utils
import botocore.utils as _botocore_utils

_botocore_utils.ContainerMetadataFetcher.TIMEOUT_SECONDS = 10
_botocore_utils.ContainerMetadataFetcher.RETRY_ATTEMPTS = 5
_aiobotocore_utils.AioContainerMetadataFetcher.TIMEOUT_SECONDS = 10
_aiobotocore_utils.AioContainerMetadataFetcher.RETRY_ATTEMPTS = 5

import aioboto3  # noqa: E402
from botocore.config import Config  # noqa: E402

from nexus_backend.config import settings  # noqa: E402
from nexus_backend.errors import ResourceNotFound  # noqa: E402
from nexus_backend.observability.logging import get_logger  # noqa: E402

log = get_logger(__name__)


# Botocore Config governs the *S3 API call* timeouts/retries (separate from
# credential fetch). Adaptive retries back off on throttling.
_CLIENT_CONFIG = Config(
    connect_timeout=5,
    read_timeout=30,
    retries={"max_attempts": 5, "mode": "adaptive"},
)


class S3Service:
    """Long-lived S3 client. Open once at startup, reuse for all requests.

    Why this matters: aioboto3 wraps RefreshableCredentials which auto-refresh
    in a background thread BEFORE they expire — but only while the client/
    session is alive. With a per-request client (the old pattern) the
    credential lifecycle was tied to each request, so the ~6h rotation could
    fall in the hot path and timeout the metadata endpoint
    (169.254.170.2/v2/credentials/<id>).
    """

    def __init__(self) -> None:
        self._session = aioboto3.Session()
        self._cm: Any = None
        self._client_obj: Any = None

    def _client_kwargs(self) -> dict[str, Any]:
        kw: dict[str, Any] = {"region_name": settings.aws_region, "config": _CLIENT_CONFIG}
        if settings.aws_endpoint_url:
            kw["endpoint_url"] = settings.aws_endpoint_url
        if settings.aws_access_key_id:
            kw["aws_access_key_id"] = settings.aws_access_key_id
        if settings.aws_secret_access_key:
            kw["aws_secret_access_key"] = settings.aws_secret_access_key
        return kw

    async def connect(self) -> None:
        """Open the long-lived S3 client. Idempotent."""
        if self._client_obj is not None:
            return
        self._cm = self._session.client("s3", **self._client_kwargs())
        self._client_obj = await self._cm.__aenter__()
        # Touch creds + bucket so any failure surfaces at startup, not on
        # the first user request.
        try:
            await self._client_obj.head_bucket(Bucket=settings.s3_receipts_bucket)
        except Exception as exc:
            log.warning("s3.connect_head_bucket_failed", error=str(exc))

    async def close(self) -> None:
        if self._cm is None:
            return
        try:
            await self._cm.__aexit__(None, None, None)
        except Exception as exc:
            log.warning("s3.close_failed", error=str(exc))
        finally:
            self._cm = None
            self._client_obj = None

    @property
    def _s3(self) -> Any:
        """Internal: the live aioboto3 client. Raises if connect() wasn't called."""
        if self._client_obj is None:
            raise RuntimeError("s3_service not connected — call connect() at startup")
        return self._client_obj

    # ── Public API ────────────────────────────────────────────────────

    async def upload_bytes(
        self,
        *,
        bucket: str,
        key: str,
        data: bytes,
        content_type: str,
    ) -> None:
        await self._s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)

    async def upload_stream(
        self,
        *,
        bucket: str,
        key: str,
        stream: AsyncIterator[bytes],
        content_type: str,
    ) -> None:
        body = b"".join([chunk async for chunk in stream])
        await self.upload_bytes(bucket=bucket, key=key, data=body, content_type=content_type)

    async def download_bytes(self, *, bucket: str, key: str) -> bytes:
        """Read object body fully into memory. Used by /expenses/{id}/receipt."""
        try:
            obj = await self._s3.get_object(Bucket=bucket, Key=key)
        except self._s3.exceptions.NoSuchKey as exc:
            raise ResourceNotFound(f"s3 object {bucket}/{key} not found") from exc
        return await obj["Body"].read()

    async def head(self, *, bucket: str, key: str) -> bool:
        try:
            await self._s3.head_object(Bucket=bucket, Key=key)
            return True
        except Exception:
            return False

    async def presign_get(self, *, bucket: str, key: str, expires: int = 600) -> str:
        return await self._s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires,
        )


s3_service = S3Service()
