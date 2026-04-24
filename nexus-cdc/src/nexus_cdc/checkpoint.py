"""DynamoDB-backed resume tokens.

Table schema (one item per watched collection):
    PK: collection (S)
    resume_token: M | S         # Mongo ChangeStream resumeAfter token (bson dict → json string)
    last_ts_ms:   N             # observed wallTime/clusterTime last flush, for monitoring
    updated_at:   S             # ISO string, for monitoring
"""
from __future__ import annotations

import json
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError


class OffsetStore:
    def __init__(self, table_name: str, region: str, endpoint_url: str | None = None) -> None:
        kwargs: dict[str, Any] = {"region_name": region}
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        self._ddb = boto3.resource("dynamodb", **kwargs)
        self._table = self._ddb.Table(table_name)

    def get(self, collection: str) -> dict[str, Any] | None:
        try:
            resp = self._table.get_item(Key={"collection": collection})
        except ClientError:
            return None
        item = resp.get("Item")
        if not item:
            return None
        token_raw = item.get("resume_token")
        if isinstance(token_raw, str):
            try:
                item["resume_token"] = json.loads(token_raw)
            except json.JSONDecodeError:
                item["resume_token"] = None
        return item

    def put(self, collection: str, resume_token: dict[str, Any] | None, last_ts_ms: int) -> None:
        item = {
            "collection": collection,
            "resume_token": json.dumps(resume_token) if resume_token else "",
            "last_ts_ms": last_ts_ms,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._table.put_item(Item=item)

    def mark_bootstrap_complete(self, collection: str, cluster_time_ms: int) -> None:
        """Called after full-sync finishes. resume_token stays empty so the
        next watch() falls back to startAtOperationTime."""
        self._table.put_item(
            Item={
                "collection": collection,
                "resume_token": "",
                "last_ts_ms": cluster_time_ms,
                "bootstrap_completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )
