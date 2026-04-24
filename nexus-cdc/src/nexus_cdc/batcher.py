"""Batching writer: buffers CDC events per collection and flushes to S3 JSONL.gz.

Flush triggers:
 - Size: when buffer reaches `batch_max_events`.
 - Time: when `batch_max_seconds` elapses since the first event in the buffer.

The batcher is single-threaded per collection — safe to call from the thread
that owns the change stream cursor.

Layout (matches Databricks Autoloader Hive-style partition discovery):
    s3://{bucket}/{collection}/year=YYYY/month=MM/day=DD/hour=HH/batch_<ULID>.jsonl.gz

Use `batch_<ULID>` so files sort chronologically on S3 list — helps debugging
and `cloudFiles.useIncrementalListing`.
"""
from __future__ import annotations

import gzip
import io
import json
import time
from datetime import UTC, datetime
from typing import Any

import boto3
from ulid import ULID

from nexus_cdc.observability import get_logger

log = get_logger("nexus_cdc.batcher")


class S3Batcher:
    def __init__(
        self,
        *,
        bucket: str,
        collection: str,
        region: str,
        max_events: int,
        max_seconds: int,
        endpoint_url: str | None = None,
    ) -> None:
        self.bucket = bucket
        self.collection = collection
        self.max_events = max_events
        self.max_seconds = max_seconds
        kwargs: dict[str, Any] = {"region_name": region}
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        self._s3 = boto3.client("s3", **kwargs)
        self._buffer: list[dict[str, Any]] = []
        self._opened_at: float | None = None

    def add(self, event: dict[str, Any]) -> None:
        if not self._buffer:
            self._opened_at = time.monotonic()
        self._buffer.append(event)
        if len(self._buffer) >= self.max_events:
            self.flush(reason="size")

    def tick(self) -> None:
        """Call periodically to flush time-based batches."""
        if not self._buffer or self._opened_at is None:
            return
        if time.monotonic() - self._opened_at >= self.max_seconds:
            self.flush(reason="time")

    def flush(self, *, reason: str = "manual") -> str | None:
        if not self._buffer:
            return None
        now = datetime.now(UTC)
        key = (
            f"{self.collection}/"
            f"year={now.year:04d}/month={now.month:02d}/day={now.day:02d}/hour={now.hour:02d}/"
            f"batch_{ULID()}.jsonl.gz"
        )
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
            for ev in self._buffer:
                gz.write(json.dumps(ev, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
                gz.write(b"\n")
        payload = buf.getvalue()
        try:
            self._s3.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=payload,
                ContentType="application/x-ndjson",
                ContentEncoding="gzip",
            )
        except Exception:
            log.exception(
                "s3_put_failed",
                collection=self.collection,
                key=key,
                events=len(self._buffer),
            )
            raise
        log.info(
            "batch_flushed",
            collection=self.collection,
            key=key,
            events=len(self._buffer),
            bytes=len(payload),
            reason=reason,
        )
        self._buffer.clear()
        self._opened_at = None
        return key

    def dlq(self, event: dict[str, Any], error: str) -> None:
        """Write a single event to the DLQ prefix for later inspection."""
        now = datetime.now(UTC)
        key = (
            f"dlq/{self.collection}/"
            f"year={now.year:04d}/month={now.month:02d}/day={now.day:02d}/"
            f"evt_{ULID()}.json"
        )
        payload = json.dumps({"event": event, "error": error, "ts": now.isoformat()})
        try:
            self._s3.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=payload.encode("utf-8"),
                ContentType="application/json",
            )
        except Exception:
            log.exception("dlq_put_failed", collection=self.collection, key=key)
