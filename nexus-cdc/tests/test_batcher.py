from __future__ import annotations

import gzip
import io
import json
import time

import boto3
import pytest
from moto import mock_aws

from nexus_cdc.batcher import S3Batcher


@pytest.fixture
def s3_bucket():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-cdc")
        yield client, "test-cdc"


def _read_key(client, bucket, key):
    body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    with gzip.GzipFile(fileobj=io.BytesIO(body)) as gz:
        return [json.loads(line) for line in gz.read().splitlines()]


def test_flush_on_size(s3_bucket):
    client, bucket = s3_bucket
    batcher = S3Batcher(
        bucket=bucket,
        collection="expenses",
        region="us-east-1",
        max_events=3,
        max_seconds=60,
    )
    for i in range(3):
        batcher.add({"expense_id": f"exp_{i}", "__op": "c", "__source_ts_ms": i, "__deleted": False})

    keys = client.list_objects_v2(Bucket=bucket).get("Contents", [])
    assert len(keys) == 1
    events = _read_key(client, bucket, keys[0]["Key"])
    assert [e["expense_id"] for e in events] == ["exp_0", "exp_1", "exp_2"]
    assert keys[0]["Key"].startswith("expenses/year=")


def test_flush_on_time(s3_bucket, monkeypatch):
    client, bucket = s3_bucket
    batcher = S3Batcher(
        bucket=bucket,
        collection="receipts",
        region="us-east-1",
        max_events=100,
        max_seconds=0,  # flush on every tick
    )
    batcher.add({"receipt_id": "r1", "__op": "c", "__source_ts_ms": 1, "__deleted": False})
    # Force elapsed > max_seconds
    batcher._opened_at = time.monotonic() - 10
    batcher.tick()
    keys = client.list_objects_v2(Bucket=bucket).get("Contents", [])
    assert len(keys) == 1


def test_empty_flush_is_noop(s3_bucket):
    client, bucket = s3_bucket
    batcher = S3Batcher(
        bucket=bucket,
        collection="receipts",
        region="us-east-1",
        max_events=10,
        max_seconds=10,
    )
    assert batcher.flush() is None
    keys = client.list_objects_v2(Bucket=bucket).get("Contents", [])
    assert len(keys) == 0


def test_dlq_writes_to_dlq_prefix(s3_bucket):
    client, bucket = s3_bucket
    batcher = S3Batcher(
        bucket=bucket, collection="expenses", region="us-east-1", max_events=10, max_seconds=10
    )
    batcher.dlq({"expense_id": "bad"}, "schema_mismatch")
    keys = [k["Key"] for k in client.list_objects_v2(Bucket=bucket).get("Contents", [])]
    assert any(k.startswith("dlq/expenses/") for k in keys)
