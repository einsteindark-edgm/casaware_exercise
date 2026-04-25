"""Orchestrated VS provisioning: wait endpoint ONLINE → create index → wait
READY → smoke test. Run after tearing down a colgado index/endpoint pair.

Emits one stdout line per state change (with flush) so an external monitor
sees real progress.
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

os.environ.setdefault("SSL_CERT_FILE", "/etc/ssl/cert.pem")

env: dict[str, str] = {}
for line in Path(".env").read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    env[k.strip()] = v.strip()

HOST = env["DATABRICKS_HOST"].rstrip("/")
TOKEN = env["DATABRICKS_TOKEN"]
ENDPOINT = "nexus-vs-dev"
INDEX = "nexus_dev.vector.expense_chunks_index"
SOURCE = "nexus_dev.gold.expense_chunks"

CTX = ssl.create_default_context(cafile="/etc/ssl/cert.pem")
HDR = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}


def call(method, path, body=None, timeout=60):
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(HOST + path, data=data, headers=HDR, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=timeout, context=CTX)
        try:
            return r.status, json.loads(r.read())
        except Exception:
            return r.status, {}
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


def emit(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


# ── 1. Wait endpoint ONLINE ────────────────────────────────────────────────
emit("STAGE=endpoint_wait")
prev = ""
for i in range(60):  # 30 min max
    code, body = call("GET", f"/api/2.0/vector-search/endpoints/{ENDPOINT}")
    state = (body.get("endpoint_status") or {}).get("state") or "UNKNOWN"
    if state != prev:
        emit(f"endpoint state={state}")
        prev = state
    if state == "ONLINE":
        break
    time.sleep(30)
else:
    emit("ERROR endpoint never came ONLINE")
    sys.exit(2)

# ── 2. Create index ────────────────────────────────────────────────────────
emit("STAGE=index_create")
body = {
    "name": INDEX,
    "endpoint_name": ENDPOINT,
    "primary_key": "chunk_id",
    "index_type": "DELTA_SYNC",
    "delta_sync_index_spec": {
        "source_table": SOURCE,
        "pipeline_type": "TRIGGERED",
        "embedding_source_columns": [
            {
                "name": "chunk_text",
                "embedding_model_endpoint_name": "databricks-bge-large-en",
            }
        ],
    },
}
code, body = call("POST", "/api/2.0/vector-search/indexes", body=body)
if code != 200:
    emit(f"ERROR create index code={code} body={str(body)[:200]}")
    sys.exit(3)
emit(f"index created detailed_state={body.get('status', {}).get('detailed_state')}")

# ── 3. Wait index READY ────────────────────────────────────────────────────
emit("STAGE=index_wait")
prev = ""
for i in range(120):  # 60 min max
    code, body = call("GET", f"/api/2.0/vector-search/indexes/{INDEX}")
    s = body.get("status", {}) or {}
    snap = (
        f"ready={s.get('ready')} "
        f"state={s.get('detailed_state')} "
        f"rows={s.get('indexed_row_count')}"
    )
    if snap != prev:
        emit(f"index {snap}")
        prev = snap
    if s.get("ready"):
        break
    if (s.get("detailed_state") or "").startswith("FAILED"):
        emit(f"ERROR {s.get('message')}")
        sys.exit(4)
    time.sleep(30)
else:
    emit("ERROR index never became READY")
    sys.exit(5)

# ── 4. Smoke test ──────────────────────────────────────────────────────────
emit("STAGE=smoke_test")
body = {
    "query_text": "cafe Starbucks",
    "columns": [
        "chunk_id",
        "expense_id",
        "chunk_text",
        "amount",
        "currency",
        "vendor",
        "date",
    ],
    "num_results": 3,
    "filters_json": json.dumps({"tenant_id": "t_alpha"}),
    "query_type": "HYBRID",
}
code, body = call("GET", f"/api/2.0/vector-search/indexes/{INDEX}/query", body=body)
# GET with body — try POST too in case
if code != 200:
    code, body = call("POST", f"/api/2.0/vector-search/indexes/{INDEX}/query", body=body)
emit(f"smoke code={code}")
rows = (body.get("result") or {}).get("data_array") or []
emit(f"smoke rows={len(rows)}")
for r in rows:
    emit(f"  match: {r}")

emit("✅ DONE")
