"""Read the user-reported fields of an expense for the auditor workflow.

Dev: reads from MongoDB `expenses` (populated by the backend on POST).
Prod: reads from Databricks `silver.expenses` (populated by CDC).

Output is normalised to JSON-safe primitives (ISO strings for dates) so the
downstream compare_fields activity is source-agnostic.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient
from temporalio import activity

from nexus_orchestration.config import settings

_mongo: AsyncIOMotorClient | None = None


def _db():
    global _mongo
    if _mongo is None:
        _mongo = AsyncIOMotorClient(settings.mongodb_uri, tz_aware=True)
    return _mongo[settings.mongodb_db]


def _iso_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


async def _from_mongo(expense_id: str, tenant_id: str) -> dict[str, Any]:
    doc = await _db().expenses.find_one(
        {"expense_id": expense_id, "tenant_id": tenant_id},
        {"_id": 0, "amount": 1, "currency": 1, "date": 1, "vendor": 1, "category": 1},
    )
    if not doc:
        return {}
    return {
        "amount": doc.get("amount"),
        "currency": doc.get("currency"),
        "date": _iso_date(doc.get("date")),
        "vendor": doc.get("vendor"),
        "category": doc.get("category"),
    }


def _from_databricks_sql(expense_id: str, tenant_id: str) -> dict[str, Any]:
    # Imported lazily so dev envs without databricks-sql-connector installed still load.
    from databricks import sql  # type: ignore[import-not-found]

    with sql.connect(
        server_hostname=settings.databricks_host,
        http_path=f"/sql/1.0/warehouses/{settings.databricks_warehouse_id}",
        access_token=settings.databricks_token,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT amount, currency, date, vendor, category
                FROM {settings.databricks_catalog}.silver.expenses
                WHERE expense_id = %(expense_id)s AND tenant_id = %(tenant_id)s
                """,
                {"expense_id": expense_id, "tenant_id": tenant_id},
            )
            row = cur.fetchone()
            if row is None:
                return {}
            cols = [d[0] for d in cur.description]
            out = dict(zip(cols, row, strict=False))
            out["date"] = _iso_date(out.get("date"))
            return out


@activity.defn(name="query_expense_for_validation")
async def query_expense_for_validation(inp: dict[str, Any]) -> dict[str, Any]:
    expense_id = inp["expense_id"]
    tenant_id = inp["tenant_id"]
    assert tenant_id, "tenant_id is required for multi-tenant isolation"

    if settings.audit_source == "databricks":
        # Databricks SQL connector is sync — run it in a thread.
        import asyncio

        return await asyncio.to_thread(_from_databricks_sql, expense_id, tenant_id)

    return await _from_mongo(expense_id, tenant_id)
