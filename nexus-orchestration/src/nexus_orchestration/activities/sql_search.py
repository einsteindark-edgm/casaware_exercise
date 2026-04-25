"""Structured SQL search activity — the STRUCTURED leg of hybrid RAG.

Runs parametrized SQL against `${catalog}.gold.expense_audit`. Tenant filter
ALWAYS comes from the workflow input; any LLM-supplied tenant field is
ignored. SQL is never concatenated from user/LLM strings — every filter
becomes a named bind parameter.

Return shape:
    {
        "aggregate_kind": "sum" | "count" | "list",
        "aggregate_value": float | int | None,
        "currency": "COP" | "USD" | "EUR" | None,
        "rows": [
            {expense_id, vendor, amount, currency, date, category, link},
            ...
        ],
        "row_count_total": int,
    }

On error (no filters, invalid aggregate) returns:
    {"error": "<message>"}
so the LLM can retry without crashing the workflow.
"""
from __future__ import annotations

import asyncio
from typing import Any

from temporalio import activity

from nexus_orchestration.activities._fakes import fake_sql_search
from nexus_orchestration.config import settings
from nexus_orchestration.observability.rag_metrics import tool_call_span

# ── Guardrails ──────────────────────────────────────────────────────────────

_ALLOWED_CATEGORIES = {"travel", "food", "lodging", "office", "other"}
_ALLOWED_CURRENCIES = {"COP", "USD", "EUR"}
_ALLOWED_AGGREGATES = {"sum", "count", "list"}
_MAX_LIMIT = 50
# When aggregate=count/sum, we still fetch a small sample of real rows so the
# LLM has actual expense_ids to cite. Without this it would either invent IDs
# or refuse to cite — both bad UX. Keep it small to bound payload size.
_AGGREGATE_SAMPLE_LIMIT = 10


def build_sql(
    tool_input: dict[str, Any],
    tenant_filter: str,
    catalog: str,
) -> tuple[str, dict[str, Any], str, str]:
    """Pure SQL builder. Returns (sql, bind_params, aggregate_kind, where_clause).

    Raises ValueError if no filters are provided or if an input is invalid.
    No f-string concat of user data — only column names and the catalog
    (server-controlled) are interpolated.
    """
    assert tenant_filter, "tenant_filter is required"

    params: dict[str, Any] = {"tenant_id": tenant_filter}
    where: list[str] = ["tenant_id = %(tenant_id)s"]
    user_filter_count = 0

    vendor = tool_input.get("vendor")
    if vendor:
        # Substring match (case-insensitive) so "uber" matches
        # "Uber Technologies Inc" or "UBER BV". The user-supplied fragment
        # stays as a bound parameter — only the % wildcards are concatenated
        # in SQL, never the value itself.
        params["vendor"] = f"%{str(vendor).strip().lower()}%"
        where.append("LOWER(final_vendor) LIKE %(vendor)s")
        user_filter_count += 1

    category = tool_input.get("category")
    if category:
        cat = str(category).strip().lower()
        if cat not in _ALLOWED_CATEGORIES:
            raise ValueError(f"invalid category: {category}")
        params["category"] = cat
        where.append("LOWER(category) = %(category)s")
        user_filter_count += 1

    amount_min = tool_input.get("amount_min")
    if amount_min is not None:
        params["amount_min"] = float(amount_min)
        where.append("final_amount >= %(amount_min)s")
        user_filter_count += 1

    amount_max = tool_input.get("amount_max")
    if amount_max is not None:
        params["amount_max"] = float(amount_max)
        where.append("final_amount <= %(amount_max)s")
        user_filter_count += 1

    date_from = tool_input.get("date_from")
    if date_from:
        params["date_from"] = str(date_from)
        where.append("final_date >= %(date_from)s")
        user_filter_count += 1

    date_to = tool_input.get("date_to")
    if date_to:
        params["date_to"] = str(date_to)
        where.append("final_date <= %(date_to)s")
        user_filter_count += 1

    currency = tool_input.get("currency")
    if currency:
        cur = str(currency).strip().upper()
        if cur not in _ALLOWED_CURRENCIES:
            raise ValueError(f"invalid currency: {currency}")
        params["currency"] = cur
        where.append("final_currency = %(currency)s")
        user_filter_count += 1

    if user_filter_count == 0:
        raise ValueError(
            "at least one filter is required (vendor, category, amount_*, "
            "date_*, currency)"
        )

    aggregate = str(tool_input.get("aggregate") or "list").lower()
    if aggregate not in _ALLOWED_AGGREGATES:
        raise ValueError(f"invalid aggregate: {aggregate}")

    raw_limit = tool_input.get("limit", 20)
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid limit: {raw_limit}") from exc
    limit = max(1, min(limit, _MAX_LIMIT))

    where_clause = " AND ".join(where)
    table = f"{catalog}.gold.expense_audit"

    if aggregate == "sum":
        sql = (
            f"SELECT SUM(final_amount) AS total, "  # noqa: S608 — identifiers only
            f"ANY_VALUE(final_currency) AS currency, "
            f"COUNT(*) AS n "
            f"FROM {table} WHERE {where_clause}"
        )
    elif aggregate == "count":
        sql = (
            f"SELECT COUNT(*) AS n "  # noqa: S608
            f"FROM {table} WHERE {where_clause}"
        )
    else:  # list
        sql = (
            f"SELECT expense_id, final_vendor AS vendor, "  # noqa: S608
            f"final_amount AS amount, final_currency AS currency, "
            f"final_date AS date_, category "
            f"FROM {table} WHERE {where_clause} "
            f"ORDER BY final_date DESC "
            f"LIMIT {limit}"
        )

    # Invariant: tenant clause present. Belt-and-braces — unit tested.
    assert "tenant_id = %(tenant_id)s" in sql

    return sql, params, aggregate, where_clause


def _rows_with_link(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        eid = r.get("expense_id")
        if not eid:
            continue
        item = dict(r)
        item["link"] = f"/expenses/{eid}"
        item["_source"] = "sql"
        out.append(item)
    return out


def _normalize_sample_rows(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize the count/sum companion sample to the same shape as the
    list aggregate so downstream code (citations, LLM) is uniform."""
    normalized: list[dict[str, Any]] = []
    for r in raw:
        normalized.append(
            {
                "expense_id": r.get("expense_id"),
                "vendor": r.get("vendor"),
                "amount": float(r["amount"]) if r.get("amount") is not None else None,
                "currency": r.get("currency"),
                "date": str(r["date_"]) if r.get("date_") is not None else None,
                "category": r.get("category"),
            }
        )
    return _rows_with_link(normalized)


def _run_databricks_sql(
    sql: str,
    params: dict[str, Any],
    aggregate_kind: str,
    catalog: str,
    where_clause: str,
) -> dict[str, Any]:
    from databricks import sql as dbsql  # type: ignore[import-not-found]

    with dbsql.connect(
        server_hostname=settings.databricks_host,
        http_path=f"/sql/1.0/warehouses/{settings.databricks_warehouse_id}",
        access_token=settings.databricks_token,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            fetched = cur.fetchall()

            # For count/sum we also need real rows so the LLM can cite without
            # inventing IDs. Run a small bounded sample query in the SAME
            # connection (no extra round-trip warmup).
            sample_rows: list[dict[str, Any]] = []
            if aggregate_kind in ("sum", "count"):
                sample_sql = (
                    f"SELECT expense_id, final_vendor AS vendor, "  # noqa: S608
                    f"final_amount AS amount, final_currency AS currency, "
                    f"final_date AS date_, category "
                    f"FROM {catalog}.gold.expense_audit WHERE {where_clause} "
                    f"ORDER BY final_date DESC "
                    f"LIMIT {_AGGREGATE_SAMPLE_LIMIT}"
                )
                cur.execute(sample_sql, params)
                scols = [d[0] for d in cur.description]
                sample_rows = [
                    dict(zip(scols, r, strict=False)) for r in cur.fetchall()
                ]

    rows = [dict(zip(cols, r, strict=False)) for r in fetched]

    if aggregate_kind == "sum":
        row = rows[0] if rows else {}
        total = row.get("total")
        return {
            "aggregate_kind": "sum",
            "aggregate_value": float(total) if total is not None else 0.0,
            "currency": row.get("currency"),
            "rows": _normalize_sample_rows(sample_rows),
            "row_count_total": int(row.get("n") or 0),
        }
    if aggregate_kind == "count":
        row = rows[0] if rows else {}
        return {
            "aggregate_kind": "count",
            "aggregate_value": int(row.get("n") or 0),
            "currency": None,
            "rows": _normalize_sample_rows(sample_rows),
            "row_count_total": int(row.get("n") or 0),
        }

    # list
    normalized = []
    for r in rows:
        normalized.append(
            {
                "expense_id": r.get("expense_id"),
                "vendor": r.get("vendor"),
                "amount": float(r["amount"]) if r.get("amount") is not None else None,
                "currency": r.get("currency"),
                "date": str(r["date_"]) if r.get("date_") is not None else None,
                "category": r.get("category"),
            }
        )
    enriched = _rows_with_link(normalized)
    return {
        "aggregate_kind": "list",
        "aggregate_value": None,
        "currency": None,
        "rows": enriched,
        "row_count_total": len(enriched),
    }


@activity.defn(name="search_expenses_structured")
async def search_expenses_structured(inp: dict[str, Any]) -> dict[str, Any]:
    tenant_filter = inp.get("tenant_filter")
    assert tenant_filter, "tenant_filter is required"

    # Drop any tenant-like keys the LLM may have supplied. Defense in depth.
    tool_input = {
        k: v
        for k, v in inp.items()
        if k not in {"tenant_filter", "tenant_id", "tenantId", "tenant"}
    }

    try:
        workflow_id = activity.info().workflow_id
    except Exception:
        workflow_id = None

    with tool_call_span(
        tool="search_expenses_structured",
        workflow_id=workflow_id,
    ) as span:
        try:
            sql_text, params, aggregate, where_clause = build_sql(
                tool_input,
                tenant_filter=tenant_filter,
                catalog=settings.databricks_catalog,
            )
        except ValueError as err:
            span["outcome"] = "invalid_input"
            return {"error": str(err)}

        if settings.use_fake_sql_search:
            result = fake_sql_search(
                tool_input=tool_input,
                tenant_filter=tenant_filter,
                aggregate_kind=aggregate,
            )
            result["rows"] = _rows_with_link(result.get("rows", []))
        else:
            result = await asyncio.to_thread(
                _run_databricks_sql,
                sql_text,
                params,
                aggregate,
                settings.databricks_catalog,
                where_clause,
            )

        span["row_count"] = int(result.get("row_count_total") or 0)
        return result
