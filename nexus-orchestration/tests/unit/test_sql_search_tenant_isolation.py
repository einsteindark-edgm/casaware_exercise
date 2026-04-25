"""Adversarial tests: LLM tries to escape tenant isolation.

These validate D.10's critical blocker: no matter what the LLM puts in its
tool_input, `tenant_filter` must come from the workflow.
"""
from __future__ import annotations

import pytest

from nexus_orchestration.activities.sql_search import (
    build_sql,
    search_expenses_structured,
)


def test_build_sql_requires_tenant_filter():
    with pytest.raises(AssertionError):
        build_sql({"vendor": "Uber"}, tenant_filter="", catalog="nexus_dev")


def test_build_sql_ignores_tenant_keys_in_input():
    # Input also contains a tenant_id claim — the builder doesn't look at it,
    # the only tenant that ends up in the SQL is the one we pass explicitly.
    sql, params, _ = build_sql(
        {"vendor": "Uber", "tenant_id": "t_ATTACKER"},
        tenant_filter="t_OWNER",
        catalog="nexus_dev",
    )
    assert params["tenant_id"] == "t_OWNER"
    assert "t_ATTACKER" not in sql
    assert "t_ATTACKER" not in str(params.values())


@pytest.mark.asyncio
async def test_activity_drops_llm_supplied_tenant_claims(monkeypatch):
    """The activity wrapper must scrub tenant_id-shaped keys from the
    LLM-supplied input before building SQL, even if the builder itself would
    ignore them.
    """
    captured: dict = {}

    async def fake_to_thread(fn, sql, params, agg):
        captured["params"] = params
        return {
            "aggregate_kind": agg,
            "aggregate_value": None,
            "currency": None,
            "rows": [],
            "row_count_total": 0,
        }

    import nexus_orchestration.activities.sql_search as mod

    monkeypatch.setattr(mod.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(mod.settings, "fake_sql_search", False, raising=False)
    monkeypatch.setattr(mod.settings, "fake_providers", False, raising=False)

    await search_expenses_structured(
        {
            "vendor": "Uber",
            "tenant_id": "t_ATTACKER",
            "tenantId": "t_ATTACKER",
            "tenant": "t_ATTACKER",
            "tenant_filter": "t_OWNER",
        }
    )
    assert captured["params"]["tenant_id"] == "t_OWNER"


@pytest.mark.asyncio
async def test_activity_returns_error_on_empty_filters():
    result = await search_expenses_structured({"tenant_filter": "t_OWNER"})
    assert "error" in result
