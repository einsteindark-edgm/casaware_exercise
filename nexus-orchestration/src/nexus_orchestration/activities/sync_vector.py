"""Trigger a vector index refresh for a newly approved expense.

Two backends, selected by `VECTOR_BACKEND` env (matches activities/vector_search.py):

  - "managed" → calls Mosaic AI Vector Search `idx.sync()`. Best when the
                workspace tier supports the auto-managed embedding pipeline.

  - "local"   → reads the chunk row from `gold.expense_chunks`, embeds the
                `chunk_text` via Foundation Models API, and UPDATEs the
                `embedding` column. The chat's vector_similarity_search then
                reads pre-computed embeddings and only embeds the user query.

Best-effort: if anything fails the audit MUST NOT fail. Vector search is a
downstream consumer, not part of the financial source of truth.
"""
from __future__ import annotations

import asyncio
import json
import os
import ssl
import urllib.error
import urllib.request
from typing import Any

from temporalio import activity

from nexus_orchestration.config import settings
from nexus_orchestration.observability.logging import get_logger

log = get_logger(__name__)

VECTOR_BACKEND = os.environ.get("VECTOR_BACKEND", "local").lower()


@activity.defn(name="trigger_vector_sync")
async def trigger_vector_sync(inp: dict[str, Any] | None = None) -> dict[str, Any]:
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
        return await asyncio.to_thread(_local_sync_one, expense_id, tenant_id)
    except Exception as exc:  # never fail the audit
        log.error(
            "vector_sync.failed",
            error=type(exc).__name__,
            message=str(exc),
            expense_id=expense_id,
            tenant_id=tenant_id,
        )
        return {"status": "failed", "error": type(exc).__name__}


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
    """Embed the chunk_text and MERGE into gold.expense_embeddings.

    `gold.expense_chunks` is a DLT MATERIALIZED VIEW (UPDATE/MERGE not
    allowed on it). Embeddings live in a SEPARATE managed table
    `gold.expense_embeddings (chunk_id, tenant_id, embedding ARRAY<FLOAT>)`
    that this activity owns. vector_search.py LEFT JOINs both.

    Polls gold.expense_chunks for up to ~18 minutes because the gold DLT
    pipeline runs every 10 min — the row may not have arrived by the time
    the workflow approves the expense (typically 6s after).
    Idempotent: MERGE recomputes embedding if it already exists, so an
    HITL re-resolve picks up the new chunk_text.
    """
    import time as _time

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
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {catalog}.gold.expense_embeddings (
                    chunk_id STRING NOT NULL,
                    tenant_id STRING NOT NULL,
                    embedding ARRAY<FLOAT>,
                    updated_at TIMESTAMP
                ) USING DELTA
                PARTITIONED BY (tenant_id)
                """
            )

            row = None
            for attempt in range(18):
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
                _time.sleep(60)
            if not row:
                log.warning(
                    "vector_sync.row_not_in_gold_after_polling",
                    expense_id=expense_id,
                    tenant_id=tenant_id,
                    attempts=18,
                )
                return {"status": "timed_out_not_in_gold"}

            chunk_id, chunk_text = row[0], row[1] or ""
            embedding = _embed([chunk_text])[0]

            # Spark's array<float> bind is hairy from databricks-sql-connector;
            # format the literal inline. Values come from the embedding
            # endpoint (no user input, no SQL injection risk).
            literal = "ARRAY(" + ",".join(f"CAST({v} AS FLOAT)" for v in embedding) + ")"
            cur.execute(
                f"""
                MERGE INTO {catalog}.gold.expense_embeddings t
                USING (SELECT
                    %(chunk_id)s AS chunk_id,
                    %(tenant_id)s AS tenant_id,
                    {literal} AS embedding,
                    current_timestamp() AS updated_at
                ) s
                ON t.chunk_id = s.chunk_id AND t.tenant_id = s.tenant_id
                WHEN MATCHED THEN UPDATE SET embedding = s.embedding, updated_at = s.updated_at
                WHEN NOT MATCHED THEN INSERT (chunk_id, tenant_id, embedding, updated_at)
                    VALUES (s.chunk_id, s.tenant_id, s.embedding, s.updated_at)
                """,
                {"chunk_id": chunk_id, "tenant_id": tenant_id},
            )

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
