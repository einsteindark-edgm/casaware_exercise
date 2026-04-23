from __future__ import annotations

from collections.abc import AsyncIterator

import aioboto3

from nexus_backend.config import settings


class S3Service:
    """Thin wrapper around aioboto3's S3 client used for receipts upload/get."""

    def __init__(self) -> None:
        self._session = aioboto3.Session()

    def _client_kwargs(self) -> dict[str, str]:
        kw: dict[str, str] = {"region_name": settings.aws_region}
        if settings.aws_endpoint_url:
            kw["endpoint_url"] = settings.aws_endpoint_url
        if settings.aws_access_key_id:
            kw["aws_access_key_id"] = settings.aws_access_key_id
        if settings.aws_secret_access_key:
            kw["aws_secret_access_key"] = settings.aws_secret_access_key
        return kw

    def _client(self):  # type: ignore[no-untyped-def]
        return self._session.client("s3", **self._client_kwargs())

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
