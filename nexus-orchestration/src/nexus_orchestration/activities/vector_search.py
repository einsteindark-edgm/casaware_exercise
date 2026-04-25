"""Vector Search activity with tenant isolation.

Tenant filter ALWAYS comes from the workflow input, never from the LLM-provided
arguments. This prevents a malicious prompt from escaping to another tenant's
data. See doc 03 §7.3.

Two backends:
  - "managed"  → Databricks Mosaic AI Vector Search (managed index).
                  Requires a working auto-managed Delta Sync pipeline. On some
                  workspace tiers this pipeline doesn't materialize → the index
                  hangs in PROVISIONING_ENDPOINT forever.
  - "local"    → SQL pull chunks → embed query via Foundation Models API →
                  cosine similarity in numpy. No managed VS needed. For dev
                  and small corpora (≲10k chunks per tenant) this is fast and
                  100% real (same `databricks-bge-large-en` embedding the
                  managed index would use).

Selected via `VECTOR_BACKEND` env (default "local" while managed VS is gated
by Databricks support).
"""
from __future__ import annotations

import os
from typing import Any

from temporalio import activity

from nexus_orchestration.activities._fakes import fake_vector_search
from nexus_orchestration.config import settings
from nexus_orchestration.observability.rag_metrics import tool_call_span

VECTOR_BACKEND = os.environ.get("VECTOR_BACKEND", "local").lower()


@activity.defn(name="vector_similarity_search")
async def vector_similarity_search(inp: dict[str, Any]) -> list[dict[str, Any]]:
    query = inp["query"]
    tenant_filter = inp["tenant_filter"]
    k = int(inp.get("k", 5))
    assert tenant_filter, "tenant_filter is required"

    try:
        workflow_id = activity.info().workflow_id
    except Exception:
        workflow_id = None

    with tool_call_span(
        tool="search_expenses_semantic",
        workflow_id=workflow_id,
    ) as span:
        if settings.use_fake_vector_search:
            result = fake_vector_search(query, tenant_filter, k)
            span["row_count"] = len(result)
            return result

        if VECTOR_BACKEND == "managed":
            result = await _managed_vector_search(query, tenant_filter, k)
        else:
            result = await _local_vector_search(query, tenant_filter, k)

        span["row_count"] = len(result)
        return result


# ── Local backend (no Databricks managed VS) ───────────────────────────────


async def _local_vector_search(
    query: str,
    tenant_filter: str,
    k: int,
) -> list[dict[str, Any]]:
    """Pull chunks from SQL, embed the query, cosine similarity in Python."""
    import asyncio

    return await asyncio.to_thread(_local_sync, query, tenant_filter, k)


def _local_sync(query: str, tenant_filter: str, k: int) -> list[dict[str, Any]]:
    """Read precomputed `embedding` column from gold.expense_chunks, fall back
    to on-the-fly embedding only for rows where it's NULL (e.g. backfill in
    progress). Always embed the user query (1 round-trip).
    """
    import json
    import math

    from databricks import sql as dbsql  # type: ignore[import-not-found]

    def _to_floats(value):
        # databricks-sql-connector returns ARRAY<FLOAT> as a JSON-encoded
        # string ("[-0.009,...]") when pyarrow isn't available. Be tolerant
        # of both shapes.
        if value is None:
            return None
        if isinstance(value, list):
            return [float(x) for x in value]
        if isinstance(value, str):
            try:
                return [float(x) for x in json.loads(value)]
            except Exception:
                return None
        return None

    catalog = settings.databricks_catalog
    host = (settings.databricks_host or "").rstrip("/").replace("https://", "")
    token = settings.databricks_token
    warehouse_id = settings.databricks_warehouse_id

    with dbsql.connect(
        server_hostname=host,
        http_path=f"/sql/1.0/warehouses/{warehouse_id}",
        access_token=token,
    ) as conn:
        with conn.cursor() as cur:
            # Embeddings live in a SEPARATE managed table because
            # gold.expense_chunks is a DLT MV (no UPDATE/MERGE allowed).
            # LEFT JOIN tolerates both: pre-computed embeddings (fast path)
            # and rows where the activity hasn't run yet (NULL → on-the-fly).
            cur.execute(
                f"""
                SELECT c.chunk_id, c.expense_id, c.chunk_text, c.amount, c.currency,
                       c.vendor, c.date, c.category, e.embedding
                FROM {catalog}.gold.expense_chunks c
                LEFT JOIN {catalog}.gold.expense_embeddings e
                  ON c.chunk_id = e.chunk_id AND c.tenant_id = e.tenant_id
                WHERE c.tenant_id = %(tenant_id)s
                """,
                {"tenant_id": tenant_filter},
            )
            cols = [d[0] for d in cur.description]
            chunks = [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]

    if not chunks:
        return []

    # Normalise embedding column → list[float] | None.
    for c in chunks:
        c["embedding"] = _to_floats(c.get("embedding"))

    # Bucket: rows that already have an embedding vs rows that don't.
    missing_idx = [i for i, c in enumerate(chunks) if not c.get("embedding")]
    texts_to_embed = [query] + [chunks[i].get("chunk_text") or "" for i in missing_idx]
    embeddings = _embed_batch(texts_to_embed)
    query_vec = embeddings[0]
    for offset, i in enumerate(missing_idx):
        chunks[i]["embedding"] = embeddings[1 + offset]

    def _cos(a, b) -> float:
        a = list(a)
        b = list(b)
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return dot / (na * nb) if na and nb else 0.0

    scored = [(_cos(query_vec, c["embedding"]), c) for c in chunks if c.get("embedding")]
    scored.sort(key=lambda t: t[0], reverse=True)

    out: list[dict[str, Any]] = []
    for score, chunk in scored[:k]:
        eid = chunk.get("expense_id")
        if not eid:
            continue
        out.append(
            {
                "chunk_id": chunk.get("chunk_id"),
                "expense_id": eid,
                "chunk_text": chunk.get("chunk_text"),
                "amount": chunk.get("amount"),
                "currency": chunk.get("currency"),
                "vendor": chunk.get("vendor"),
                "date": chunk.get("date"),
                "category": chunk.get("category"),
                "score": round(float(score), 4),
                "link": f"/expenses/{eid}",
                "_source": "semantic",
            }
        )
    return out


def _embed_batch(texts: list[str]) -> list[list[float]]:
    """Call the Databricks Foundation Models embedding endpoint.

    Single request with a list of inputs (more efficient than N round-trips).
    Returns a list of 1024-dim float vectors aligned with `texts`.
    """
    import json
    import ssl
    import urllib.request

    host = (settings.databricks_host or "").rstrip("/")
    if not host.startswith("http"):
        host = f"https://{host}"
    token = settings.databricks_token

    # Use the system trust store explicitly — corp MITM proxies sometimes
    # break the python certifi default. SSL_CERT_FILE env var also works.
    cafile = os.environ.get("SSL_CERT_FILE") or "/etc/ssl/cert.pem"
    try:
        ctx = ssl.create_default_context(cafile=cafile)
    except FileNotFoundError:
        ctx = ssl.create_default_context()

    req_body = json.dumps({"input": texts}).encode("utf-8")
    req = urllib.request.Request(
        f"{host}/serving-endpoints/databricks-bge-large-en/invocations",
        data=req_body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        payload = json.loads(resp.read())

    # OpenAI-compatible shape: {"data": [{"embedding": [...]}, ...]}
    data = payload.get("data") or payload.get("predictions") or []
    embeddings: list[list[float]] = []
    for item in data:
        if isinstance(item, dict) and "embedding" in item:
            embeddings.append(item["embedding"])
        elif isinstance(item, list):
            embeddings.append(item)
    if len(embeddings) != len(texts):
        raise RuntimeError(
            f"embedding count mismatch: requested {len(texts)} got {len(embeddings)}"
        )
    return embeddings


# ── Managed backend (Databricks Mosaic AI Vector Search) ───────────────────


async def _managed_vector_search(
    query: str,
    tenant_filter: str,
    k: int,
) -> list[dict[str, Any]]:
    from databricks.vector_search.client import VectorSearchClient  # type: ignore[import-not-found]

    client = VectorSearchClient()
    index = client.get_index(
        endpoint_name=settings.databricks_vs_endpoint,
        index_name=settings.databricks_vs_index,
    )
    columns = ["chunk_id", "expense_id", "chunk_text", "date", "vendor", "amount", "currency"]
    results = index.similarity_search(
        query_text=query,
        columns=columns,
        num_results=k,
        filters={"tenant_id": tenant_filter},
        query_type="HYBRID",
    )
    data_array = results.get("result", {}).get("data_array", []) or []
    enriched: list[dict[str, Any]] = []
    for raw in data_array:
        row = dict(zip(columns, raw, strict=False)) if isinstance(raw, list) else dict(raw)
        eid = row.get("expense_id")
        if not eid:
            continue
        row["link"] = f"/expenses/{eid}"
        row["_source"] = "semantic"
        enriched.append(row)
    return enriched
