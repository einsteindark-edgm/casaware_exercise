"""Unit tests for the structured-SQL builder.

These cover the contract the LLM depends on:
- tenant_id is always pinned from the workflow, never from LLM input
- at least one filter is required
- LIMIT is forced ≤ 50 even if LLM asks for more
- vendor / category are whitelisted / case-normalized
- adversarial inputs (SQL injection via vendor) do not mutate the SQL — they
  end up as bound parameter values.
"""
from __future__ import annotations

import pytest

from nexus_orchestration.activities.sql_search import build_sql


def test_tenant_filter_always_present():
    sql, params, _ = build_sql({"vendor": "Uber"}, tenant_filter="t_ACME", catalog="nexus_dev")
    assert "tenant_id = %(tenant_id)s" in sql
    assert params["tenant_id"] == "t_ACME"


def test_no_filters_rejected():
    with pytest.raises(ValueError):
        build_sql({}, tenant_filter="t_ACME", catalog="nexus_dev")


def test_limit_clamped_to_50():
    sql, _, _ = build_sql(
        {"vendor": "Uber", "aggregate": "list", "limit": 999},
        tenant_filter="t_ACME",
        catalog="nexus_dev",
    )
    assert "LIMIT 50" in sql


def test_limit_minimum_1():
    sql, _, _ = build_sql(
        {"vendor": "Uber", "aggregate": "list", "limit": 0},
        tenant_filter="t_ACME",
        catalog="nexus_dev",
    )
    assert "LIMIT 1" in sql


def test_vendor_case_insensitive_binding():
    sql, params, _ = build_sql(
        {"vendor": "UBER"}, tenant_filter="t_ACME", catalog="nexus_dev"
    )
    assert "LOWER(final_vendor) = %(vendor)s" in sql
    assert params["vendor"] == "uber"


def test_category_whitelisted():
    build_sql({"category": "food"}, tenant_filter="t_ACME", catalog="nexus_dev")
    with pytest.raises(ValueError):
        build_sql(
            {"category": "'; DROP TABLE expenses;--"},
            tenant_filter="t_ACME",
            catalog="nexus_dev",
        )


def test_currency_whitelisted():
    with pytest.raises(ValueError):
        build_sql(
            {"currency": "XYZ"}, tenant_filter="t_ACME", catalog="nexus_dev"
        )


def test_aggregate_whitelisted():
    with pytest.raises(ValueError):
        build_sql(
            {"vendor": "Uber", "aggregate": "delete"},
            tenant_filter="t_ACME",
            catalog="nexus_dev",
        )


def test_sum_shape():
    sql, params, agg = build_sql(
        {"vendor": "Uber", "aggregate": "sum"},
        tenant_filter="t_ACME",
        catalog="nexus_dev",
    )
    assert agg == "sum"
    assert "SUM(final_amount)" in sql
    assert "LIMIT" not in sql  # aggregate doesn't need LIMIT


def test_count_shape():
    sql, _, agg = build_sql(
        {"vendor": "Uber", "aggregate": "count"},
        tenant_filter="t_ACME",
        catalog="nexus_dev",
    )
    assert agg == "count"
    assert "COUNT(*)" in sql


def test_sql_injection_attempt_in_vendor_becomes_bind_param():
    """The SQL text itself must not contain the injected payload — only the
    parameter value carries it, which the driver binds safely.
    """
    payload = "' OR 1=1 --"
    sql, params, _ = build_sql(
        {"vendor": payload}, tenant_filter="t_ACME", catalog="nexus_dev"
    )
    assert payload.lower() not in sql.lower()
    assert params["vendor"] == payload.lower()


def test_date_and_amount_range_all_bound():
    sql, params, _ = build_sql(
        {
            "date_from": "2026-03-01",
            "date_to": "2026-03-31",
            "amount_min": 10,
            "amount_max": 500,
        },
        tenant_filter="t_X",
        catalog="nexus_dev",
    )
    for key in ("date_from", "date_to", "amount_min", "amount_max"):
        assert f"%({key})s" in sql
        assert key in params
    # No user-supplied literal should land in the SQL string directly.
    assert "2026-03-01" not in sql
    assert "500" not in sql


def test_catalog_interpolated_in_table_only():
    sql, _, _ = build_sql(
        {"vendor": "Uber"}, tenant_filter="t_X", catalog="my_catalog"
    )
    assert "my_catalog.gold.expense_audit" in sql
