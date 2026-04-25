"""Local CLI runner that mirrors the Databricks notebook
`nexus-medallion/src/vector/setup_vector_search.py`.

Driven by the project .env. Idempotent — safe to re-run.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import timedelta
from pathlib import Path

# Force the system trust store so the corporate-MITM-friendly cert chain works.
os.environ.setdefault("SSL_CERT_FILE", "/etc/ssl/cert.pem")
os.environ.setdefault("REQUESTS_CA_BUNDLE", "/etc/ssl/cert.pem")

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
env: dict[str, str] = {}
for line in ENV_PATH.read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    env[k.strip()] = v.strip()

HOST = env["DATABRICKS_HOST"].rstrip("/").replace("https://", "")
TOKEN = env["DATABRICKS_TOKEN"]
WAREHOUSE_ID = env["DATABRICKS_WAREHOUSE_ID"]
CATALOG = env.get("DATABRICKS_CATALOG", "nexus_dev")
ENDPOINT_NAME = env.get("DATABRICKS_VS_ENDPOINT", "nexus-vs-dev")
INDEX_NAME = env.get(
    "DATABRICKS_VS_INDEX", f"{CATALOG}.vector.expense_chunks_index"
)
SOURCE_TABLE = f"{CATALOG}.gold.expense_chunks"
EMBEDDING_MODEL = "databricks-bge-large-en"


def step(msg: str) -> None:
    print(f"\n=== {msg} ===", flush=True)


def main() -> int:
    from databricks import sql as dbsql

    # ── 1. SQL warehouse: create source table + insert from gold.expense_audit
    step("1. Source table (gold.expense_chunks)")
    with dbsql.connect(
        server_hostname=HOST,
        http_path=f"/sql/1.0/warehouses/{WAREHOUSE_ID}",
        access_token=TOKEN,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.vector")

            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {SOURCE_TABLE} (
                    chunk_id STRING,
                    tenant_id STRING,
                    expense_id STRING,
                    chunk_text STRING,
                    amount DOUBLE,
                    currency STRING,
                    vendor STRING,
                    date STRING,
                    category STRING,
                    approved_at TIMESTAMP
                ) USING DELTA
                PARTITIONED BY (tenant_id)
                TBLPROPERTIES (delta.enableChangeDataFeed = true)
                """
            )

            # Idempotent currency column add (older table versions).
            try:
                cur.execute(
                    f"ALTER TABLE {SOURCE_TABLE} ADD COLUMNS (currency STRING)"
                )
                print("  added currency column")
            except Exception as e:
                msg = str(e).lower()
                if "already exists" in msg or "fields_already_exist" in msg:
                    print("  currency column already present")
                else:
                    raise

            print("  populating from gold.expense_audit ...")
            cur.execute(
                f"""
                INSERT OVERWRITE {SOURCE_TABLE}
                SELECT
                    CONCAT(expense_id, '_main')               AS chunk_id,
                    tenant_id,
                    expense_id,
                    CONCAT_WS(' ',
                        'Gasto en', final_vendor,
                        'por', CAST(final_amount AS STRING), final_currency,
                        'el', CAST(final_date AS STRING),
                        '. Categoria:', category, '.'
                    )                                          AS chunk_text,
                    CAST(final_amount AS DOUBLE)               AS amount,
                    final_currency                             AS currency,
                    final_vendor                               AS vendor,
                    CAST(final_date AS STRING)                 AS date,
                    category,
                    approved_at
                FROM {CATALOG}.gold.expense_audit
                """
            )

            cur.execute(f"SELECT COUNT(*) FROM {SOURCE_TABLE}")
            row_count = cur.fetchone()[0]
            print(f"  {SOURCE_TABLE}: {row_count} rows")
            if row_count == 0:
                print(
                    "  ERROR: source table is empty — gold.expense_audit "
                    "has no approved expenses. Stopping before VS setup."
                )
                return 2

            cur.execute(
                f"SELECT tenant_id, COUNT(*) FROM {SOURCE_TABLE} "
                f"GROUP BY 1 ORDER BY 2 DESC LIMIT 5"
            )
            for r in cur.fetchall():
                print(f"  tenant: {r[0]} ({r[1]} chunks)")

    # ── 2. Vector Search endpoint
    step(f"2. Endpoint ({ENDPOINT_NAME})")
    from databricks.vector_search.client import VectorSearchClient  # type: ignore[import-not-found]

    vs = VectorSearchClient(
        workspace_url=f"https://{HOST}",
        personal_access_token=TOKEN,
    )

    existing_eps = {
        e["name"] for e in vs.list_endpoints().get("endpoints", []) or []
    }
    if ENDPOINT_NAME not in existing_eps:
        print(f"  creating endpoint {ENDPOINT_NAME} (STANDARD)...")
        vs.create_endpoint(name=ENDPOINT_NAME, endpoint_type="STANDARD")
        print("  waiting for endpoint ONLINE (can take ~5-10 min)...")
        # The lib expects a timedelta in some versions, an int in others.
        try:
            vs.wait_for_endpoint(name=ENDPOINT_NAME, timeout=timedelta(seconds=1500))
        except (AttributeError, TypeError):
            vs.wait_for_endpoint(name=ENDPOINT_NAME, timeout=1500)
        print("  endpoint ONLINE")
    else:
        print(f"  endpoint {ENDPOINT_NAME} already exists — checking state")
        # Poll until ONLINE in case it's still PROVISIONING from a prior run.
        for i in range(60):
            ep = vs.get_endpoint(name=ENDPOINT_NAME) or {}
            state = (ep.get("endpoint_status") or {}).get("state") or ep.get("state")
            print(f"    [{i:02d}] state={state}")
            if state == "ONLINE":
                break
            time.sleep(30)

    # ── 3. Delta Sync Index
    step(f"3. Delta Sync Index ({INDEX_NAME})")
    existing_idxs = {
        i["name"]
        for i in vs.list_indexes(name=ENDPOINT_NAME).get("vector_indexes", []) or []
    }
    if INDEX_NAME not in existing_idxs:
        print("  creating index...")
        idx = vs.create_delta_sync_index(
            endpoint_name=ENDPOINT_NAME,
            source_table_name=SOURCE_TABLE,
            index_name=INDEX_NAME,
            primary_key="chunk_id",
            pipeline_type="TRIGGERED",
            embedding_source_column="chunk_text",
            embedding_model_endpoint_name=EMBEDDING_MODEL,
        )
        print("  index created")
    else:
        idx = vs.get_index(endpoint_name=ENDPOINT_NAME, index_name=INDEX_NAME)
        print("  index already exists — triggering sync")
        idx.sync()

    # ── 4. Wait for index READY (first sync embeds every row)
    step("4. Wait for index READY")
    for i in range(60):
        desc = idx.describe()
        status = desc.get("status", {}) or {}
        ready = status.get("ready", False)
        detailed = status.get("detailed_state", "UNKNOWN")
        message = status.get("message", "")
        print(f"  [{i:02d}] ready={ready} state={detailed} {message}")
        if ready:
            break
        time.sleep(30)
    else:
        print("  TIMEOUT waiting for index READY (took >30 min)")
        return 3

    # ── 5. Smoke test similarity_search
    step("5. Smoke test")
    # Pick the first tenant present in the source table.
    with dbsql.connect(
        server_hostname=HOST,
        http_path=f"/sql/1.0/warehouses/{WAREHOUSE_ID}",
        access_token=TOKEN,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT tenant_id FROM {SOURCE_TABLE} GROUP BY 1 LIMIT 1"
            )
            row = cur.fetchone()
            tenant = row[0] if row else None

    if not tenant:
        print("  no tenants — skipping smoke test")
        return 0

    print(f"  tenant: {tenant}")
    results = idx.similarity_search(
        query_text="cafe Starbucks",
        columns=[
            "chunk_id",
            "expense_id",
            "chunk_text",
            "amount",
            "currency",
            "vendor",
            "date",
        ],
        num_results=3,
        filters={"tenant_id": tenant},
        query_type="HYBRID",
    )
    rows = results.get("result", {}).get("data_array", []) or []
    print(f"  matches: {len(rows)}")
    for r in rows:
        print(f"    {r}")

    print("\n✅ Vector Search standup complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
