from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import aioboto3
from botocore.config import Config

from nexus_backend.config import settings
from nexus_backend.observability.logging import get_logger

log = get_logger(__name__)


# ECS task-role credential fetch + S3 calls share these timeouts. The default
# botocore connect/read timeouts (~1s) and 4 retry attempts were causing
# CredentialRetrievalError on cold tasks under load (the metadata endpoint
# 169.254.170.2 sometimes takes >1s to answer the first call). Adaptive
# retries back off when ECS metadata is slow.
_CLIENT_CONFIG = Config(
    connect_timeout=5,
    read_timeout=30,
    retries={"max_attempts": 5, "mode": "adaptive"},
)


class S3Service:
    """Thin wrapper around aioboto3's S3 client used for receipts upload/get."""

    def __init__(self) -> None:
        self._session = aioboto3.Session()

    def _client_kwargs(self) -> dict[str, Any]:
        kw: dict[str, Any] = {"region_name": settings.aws_region, "config": _CLIENT_CONFIG}
        if settings.aws_endpoint_url:
            kw["endpoint_url"] = settings.aws_endpoint_url
        if settings.aws_access_key_id:
            kw["aws_access_key_id"] = settings.aws_access_key_id
        if settings.aws_secret_access_key:
            kw["aws_secret_access_key"] = settings.aws_secret_access_key
        return kw

    def _client(self):  # type: ignore[no-untyped-def]
        return self._session.client("s3", **self._client_kwargs())

    async def prime(self) -> None:
        """Force credential resolution at startup so the first user request
        doesn't pay for the ECS metadata round-trip. head_bucket is the
        cheapest call that still triggers the credential provider chain.
        """
        try:
            async with self._client() as s3:
                await s3.head_bucket(Bucket=settings.s3_receipts_bucket)
        except Exception as exc:
            log.warning("s3.prime_failed", error=str(exc))

    async def upload_bytes(
        self,
        *,
        bucket: str,
        key: str,
        data: bytes,
        content_type: str,
    ) -> None:
        async with self._client() as s3:
            await s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)

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

    async def head(self, *, bucket: str, key: str) -> bool:
        async with self._client() as s3:
            try:
                await s3.head_object(Bucket=bucket, Key=key)
                return True
            except Exception:
                return False

    async def presign_get(self, *, bucket: str, key: str, expires: int = 600) -> str:
        async with self._client() as s3:
            return await s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=expires,
            )


s3_service = S3Service()
