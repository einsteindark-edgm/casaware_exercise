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
    """Embed the chunk_text for the given expense_id and UPDATE the column.

    Idempotent: if the row already has a non-null embedding, we recompute it
    so an HITL re-resolve picks up the new chunk_text.
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
            cur.execute(
                f"""
                SELECT chunk_id, chunk_text
                FROM {catalog}.gold.expense_chunks
                WHERE expense_id = %(expense_id)s AND tenant_id = %(tenant_id)s
                """,
                {"expense_id": expense_id, "tenant_id": tenant_id},
            )
            row = cur.fetchone()
            if not row:
                # Gold may not have CDC'd the new row yet. The next chat query
                # will fall back to on-the-fly embed for any NULL row.
                log.info(
                    "vector_sync.row_not_in_gold_yet",
                    expense_id=expense_id,
                    tenant_id=tenant_id,
                )
                return {"status": "skipped_not_in_gold"}

            chunk_id, chunk_text = row[0], row[1] or ""
            embedding = _embed([chunk_text])[0]

            # Spark's array<float> bind is hairy from databricks-sql-connector;
            # easiest is to format the literal inline. Float values are safe
            # because we generated them from the embedding endpoint (no user
            # input, no SQL injection risk).
            literal = "ARRAY(" + ",".join(f"CAST({v} AS FLOAT)" for v in embedding) + ")"
            cur.execute(
                f"""
                UPDATE {catalog}.gold.expense_chunks
                SET embedding = {literal}
                WHERE chunk_id = %(chunk_id)s
                  AND tenant_id = %(tenant_id)s
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
