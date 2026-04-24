"""Vector Search activity with tenant isolation.

Tenant filter ALWAYS comes from the workflow input, never from the LLM-provided
arguments. This prevents a malicious prompt from escaping to another tenant's
data. See doc 03 §7.3.
"""
from __future__ import annotations

from typing import Any

from temporalio import activity

from nexus_orchestration.activities._fakes import fake_vector_search
from nexus_orchestration.config import settings


@activity.defn(name="vector_similarity_search")
async def vector_similarity_search(inp: dict[str, Any]) -> list[dict[str, Any]]:
    query = inp["query"]
    tenant_filter = inp["tenant_filter"]
    k = int(inp.get("k", 5))
    assert tenant_filter, "tenant_filter is required"

    if settings.use_fake_vector_search:
        return fake_vector_search(query, tenant_filter, k)

    from databricks.vector_search.client import VectorSearchClient  # type: ignore[import-not-found]

    client = VectorSearchClient()
    index = client.get_index(
        endpoint_name=settings.databricks_vs_endpoint,
        index_name=settings.databricks_vs_index,
    )
    # STANDARD endpoint tier requires dict-style filters (string form only
    # works on STORAGE_OPTIMIZED). Dict form is backward-compatible.
    results = index.similarity_search(
        query_text=query,
        columns=["chunk_id", "expense_id", "chunk_text", "date", "vendor", "amount"],
        num_results=k,
        filters={"tenant_id": tenant_filter},
        query_type="HYBRID",
    )
    return results.get("result", {}).get("data_array", [])
