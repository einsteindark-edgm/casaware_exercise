"""Trigger a vector index refresh for a newly approved expense.

Two backends, selected by `VECTOR_BACKEND` env (matches activities/vector_search.py):

  - "managed" → calls Mosaic AI Vector Search `idx.sync()`. Best when the
                workspace tier supports the auto-managed embedding pipeline.

  - "local"   → reads the chunk row from `gold.expense_chunks`, embeds the
                `chunk_text` via Foundation Models API, and MERGEs into
                `gold.expense_embeddings`.

Implemented as a SYNC activity (per Temporal Python best practice for
blocking I/O — databricks-sql-connector + urllib are both blocking). Sync
activities run in the worker's `activity_executor` ThreadPoolExecutor; in
that context `activity.heartbeat()` works correctly. The previous async +
`asyncio.to_thread` design crashed with `RuntimeError: no running event
loop` because `activity.heartbeat()` from a to_thread worker can't reach
the activity's asyncio loop (incident: 2026-04-25, expense
01KQ37YXE34Q4JWEQ17CQRX5WQ).
"""
from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from typing import Any

from temporalio import activity
from temporalio.exceptions import CancelledError as TemporalCancelledError

from nexus_orchestration.config import settings
from nexus_orchestration.observability.logging import get_logger

log = get_logger(__name__)

VECTOR_BACKEND = os.environ.get("VECTOR_BACKEND", "local").lower()


@activity.defn(name="trigger_vector_sync")
def trigger_vector_sync(inp: dict[str, Any] | None = None) -> dict[str, Any]:
    inp = inp or {}
    expense_id = inp.get("expense_id")
    tenant_id = inp.get("tenant_id")

    if settings.use_fake_vector_search:
        log.info(
            "vector_sync.skipped_fake",
            expense_id=expense_id,
            tenant_id=tenant_id,
        )
        return {"status": "skipped_fake"}

    if not (settings.databricks_host and settings.databricks_token):
        log.warning(
            "vector_sync.skipped_unconfigured",
            expense_id=expense_id,
            tenant_id=tenant_id,
        )
        return {"status": "skipped_unconfigured"}

    try:
        if VECTOR_BACKEND == "managed":
            return _managed_sync(expense_id, tenant_id)
        return _local_sync_one(expense_id, tenant_id)
    except TemporalCancelledError:
        # Cancellation is delivered via heartbeat; let it bubble so Temporal
        # records the activity as CANCELLED rather than FAILED.
        log.info(
            "vector_sync.cancelled",
            expense_id=expense_id,
            tenant_id=tenant_id,
        )
        raise
    except Exception as exc:
        log.error(
            "vector_sync.failed",
            error=type(exc).__name__,
            message=str(exc),
            expense_id=expense_id,
            tenant_id=tenant_id,
        )
        return {"status": "failed", "error": type(exc).__name__, "message": str(exc)}


# ── Managed backend ────────────────────────────────────────────────────────


def _managed_sync(expense_id: str | None, tenant_id: str | None) -> dict[str, Any]:
    if not (settings.databricks_vs_endpoint and settings.databricks_vs_index):
        return {"status": "skipped_unconfigured"}

    from databricks.vector_search.client import (  # type: ignore[import-not-found]
        VectorSearchClient,
    )

    host = settings.databricks_host.rstrip("/")
    if not host.startswith("http"):
        host = f"https://{host}"
    client = VectorSearchClient(
        workspace_url=host,
        personal_access_token=settings.databricks_token,
        disable_notice=True,
    )
    idx = client.get_index(
        endpoint_name=settings.databricks_vs_endpoint,
        index_name=settings.databricks_vs_index,
    )
    idx.sync()
    log.info(
        "vector_sync.triggered_managed",
        expense_id=expense_id,
        tenant_id=tenant_id,
        index=settings.databricks_vs_index,
    )
    return {"status": "triggered", "backend": "managed"}


# ── Local backend ──────────────────────────────────────────────────────────


def _local_sync_one(expense_id: str | None, tenant_id: str | None) -> dict[str, Any]:
    """Embed the chunk_text and INSERT into gold.expense_embeddings.

    `gold.expense_chunks` is a DLT MATERIALIZED VIEW (UPDATE/MERGE not
    allowed on it). Embeddings live in a SEPARATE managed table
    `gold.expense_embeddings (chunk_id, tenant_id, embedding ARRAY<FLOAT>)`
    that this activity owns. vector_search.py LEFT JOINs both.

    Polls gold.expense_chunks for up to ~18 minutes because the gold DLT
    pipeline runs every 10 min — the row may not have arrived by the time
    the workflow approves the expense (typically 6s after).

    Append-only writes: hacemos INSERT puro en lugar de MERGE para evitar
    conflictos de concurrencia entre HITLs paralelos. Delta serializa
    UPDATE/MERGE/DELETE a nivel archivo (y row-level concurrency NO
    cubre INSERTs sobre la misma partición). En cambio, INSERTs
    concurrentes sólo agregan archivos nuevos sin tocar los existentes
    → no chocan. Como contrapartida, varias re-vectorizaciones del
    mismo chunk_id producen filas duplicadas; vector_search.py
    desambigua con ROW_NUMBER() ORDER BY updated_at DESC al leer.
    Compactación periódica (OPTIMIZE + dedupe) recoge la cola.
    Ver 12-delta-merge-concurrencia-y-parquet.md.
    """
    if not (expense_id and tenant_id):
        return {"status": "skipped_missing_keys"}

    from databricks import sql as dbsql  # type: ignore[import-not-found]

    catalog = settings.databricks_catalog
    host = settings.databricks_host.rstrip("/").replace("https://", "")
    token = settings.databricks_token
    warehouse = settings.databricks_warehouse_id
    if not warehouse:
        return {"status": "skipped_unconfigured"}

    with dbsql.connect(
        server_hostname=host,
        http_path=f"/sql/1.0/warehouses/{warehouse}",
        access_token=token,
    ) as conn:
        with conn.cursor() as cur:
            # Make sure the embeddings table exists. CTAS-style, idempotent.
            # TBLPROPERTIES son las que Databricks Serverless aplica por
            # default en DBR 15+; las dejamos explícitas para que ambientes
            # con runtimes viejos las hereden y para documentar la intención.
            # NOTA: la concurrencia de HITLs paralelos NO la resuelven estas
            # properties (row-level concurrency no cubre INSERT-on-same-
            # partition); la resuelve el patrón append-only + dedupe en
            # lectura. Ver 12-delta-merge-concurrencia-y-parquet.md.
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {catalog}.gold.expense_embeddings (
                    chunk_id STRING NOT NULL,
                    tenant_id STRING NOT NULL,
                    embedding ARRAY<FLOAT>,
                    updated_at TIMESTAMP
                ) USING DELTA
                PARTITIONED BY (tenant_id)
                TBLPROPERTIES (
                    'delta.enableDeletionVectors' = 'true',
                    'delta.enableRowTracking'     = 'true'
                )
                """
            )

            row = None
            for attempt in range(18):
                # `activity.heartbeat()` is safe inside a sync activity —
                # the executor thread carries the activity context. This is
                # the idiomatic Temporal pattern for blocking I/O loops.
                # Heartbeat ALSO delivers cancellation: if the workflow is
                # cancelled, the next heartbeat raises TemporalCancelledError.
                activity.heartbeat({"attempt": attempt, "phase": "polling_gold_chunks"})
                cur.execute(
                    f"""
                    SELECT chunk_id, chunk_text
                    FROM {catalog}.gold.expense_chunks
                    WHERE expense_id = %(expense_id)s AND tenant_id = %(tenant_id)s
                    """,
                    {"expense_id": expense_id, "tenant_id": tenant_id},
                )
                row = cur.fetchone()
                if row:
                    break
                time.sleep(60)
            if not row:
                log.warning(
                    "vector_sync.row_not_in_gold_after_polling",
                    expense_id=expense_id,
                    tenant_id=tenant_id,
                    attempts=18,
                )
                return {"status": "timed_out_not_in_gold"}

            chunk_id, chunk_text = row[0], row[1] or ""
            activity.heartbeat({"phase": "embedding"})
            embedding = _embed([chunk_text])[0]
            activity.heartbeat({"phase": "inserting"})

            # Spark's array<float> bind is hairy from databricks-sql-connector;
            # format the literal inline. Values come from the embedding
            # endpoint (no user input, no SQL injection risk).
            literal = "ARRAY(" + ",".join(f"CAST({v} AS FLOAT)" for v in embedding) + ")"
            insert_sql = f"""
                INSERT INTO {catalog}.gold.expense_embeddings
                    (chunk_id, tenant_id, embedding, updated_at)
                VALUES (
                    %(chunk_id)s,
                    %(tenant_id)s,
                    {literal},
                    current_timestamp()
                )
                """
            # INSERTs concurrentes en Delta no conflictan: cada writer agrega
            # archivos parquet nuevos sin tocar los existentes. Aún así
            # mantenemos un retry chico contra DELTA_CONCURRENT_APPEND para
            # cubrir el edge-case en que un OPTIMIZE/VACUUM esté corriendo
            # en paralelo (esa SÍ reescribe archivos). Backoff 1s/2s/4s.
            for ins_attempt in range(4):
                try:
                    cur.execute(insert_sql, {"chunk_id": chunk_id, "tenant_id": tenant_id})
                    break
                except Exception as ins_exc:
                    if (
                        "DELTA_CONCURRENT_APPEND" in str(ins_exc)
                        and ins_attempt < 3
                    ):
                        wait_s = 2**ins_attempt
                        log.warning(
                            "vector_sync.delta_concurrent_retry",
                            expense_id=expense_id,
                            tenant_id=tenant_id,
                            attempt=ins_attempt + 1,
                            wait_s=wait_s,
                        )
                        activity.heartbeat({"phase": "insert_retry", "attempt": ins_attempt + 1})
                        time.sleep(wait_s)
                        continue
                    raise

    log.info(
        "vector_sync.triggered_local",
        expense_id=expense_id,
        tenant_id=tenant_id,
        embedding_dim=len(embedding),
    )
    return {"status": "triggered", "backend": "local", "embedding_dim": len(embedding)}


def _embed(texts: list[str]) -> list[list[float]]:
    """Single-batch call to the Foundation Models embedding endpoint."""
    host = settings.databricks_host.rstrip("/")
    if not host.startswith("http"):
        host = f"https://{host}"
    cafile = os.environ.get("SSL_CERT_FILE") or "/etc/ssl/cert.pem"
    try:
        ctx = ssl.create_default_context(cafile=cafile)
    except FileNotFoundError:
        ctx = ssl.create_default_context()

    body = json.dumps({"input": texts}).encode("utf-8")
    req = urllib.request.Request(
        f"{host}/serving-endpoints/databricks-bge-large-en/invocations",
        data=body,
        headers={
            "Authorization": f"Bearer {settings.databricks_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        payload = json.loads(resp.read())

    data = payload.get("data") or payload.get("predictions") or []
    out: list[list[float]] = []
    for item in data:
        if isinstance(item, dict) and "embedding" in item:
            out.append(item["embedding"])
        elif isinstance(item, list):
            out.append(item)
    if len(out) != len(texts):
        raise RuntimeError(
            f"embedding count mismatch: requested {len(texts)} got {len(out)}"
        )
    return out
