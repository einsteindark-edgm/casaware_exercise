"""Bootstrap the local-backend vector search.

Idempotent: safe to re-run. Steps:
  1. ALTER TABLE nexus_dev.gold.expense_chunks ADD COLUMNS (embedding ARRAY<FLOAT>)
     — silently no-op if the column already exists.
  2. SELECT every row where embedding IS NULL, embed in batches via the
     Foundation Models endpoint, and UPDATE.
  3. Print a final summary (rows total / rows with embedding).

After this runs, the chat's vector_similarity_search returns real semantic
matches without depending on Databricks Mosaic AI Vector Search managed
indexes (which are gated by workspace tier in some accounts).
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.request
from pathlib import Path

os.environ.setdefault("SSL_CERT_FILE", "/etc/ssl/cert.pem")

env: dict[str, str] = {}
for line in Path(".env").read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    env[k.strip()] = v.strip()

HOST = env["DATABRICKS_HOST"].rstrip("/")
TOKEN = env["DATABRICKS_TOKEN"]
WAREHOUSE = env["DATABRICKS_WAREHOUSE_ID"]
CATALOG = env.get("DATABRICKS_CATALOG", "nexus_dev")
TABLE = f"{CATALOG}.gold.expense_chunks"

CTX = ssl.create_default_context(cafile="/etc/ssl/cert.pem")


def emit(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def embed(texts: list[str]) -> list[list[float]]:
    body = json.dumps({"input": texts}).encode("utf-8")
    h = HOST if HOST.startswith("http") else f"https://{HOST}"
    req = urllib.request.Request(
        f"{h}/serving-endpoints/databricks-bge-large-en/invocations",
        data=body,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60, context=CTX) as resp:
        payload = json.loads(resp.read())
    data = payload.get("data") or payload.get("predictions") or []
    out: list[list[float]] = []
    for item in data:
        if isinstance(item, dict) and "embedding" in item:
            out.append(item["embedding"])
        elif isinstance(item, list):
            out.append(item)
    if len(out) != len(texts):
        raise RuntimeError(f"embed mismatch: in={len(texts)} out={len(out)}")
    return out


def main() -> int:
    from databricks import sql as dbsql

    server = HOST.replace("https://", "")
    with dbsql.connect(
        server_hostname=server,
        http_path=f"/sql/1.0/warehouses/{WAREHOUSE}",
        access_token=TOKEN,
    ) as conn:
        with conn.cursor() as cur:
            # 1. Ensure column exists.
            emit(f"STAGE=alter_table table={TABLE}")
            try:
                cur.execute(f"ALTER TABLE {TABLE} ADD COLUMNS (embedding ARRAY<FLOAT>)")
                emit("  added embedding column")
            except Exception as e:
                if "already exists" in str(e).lower() or "fields_already_exist" in str(e).lower():
                    emit("  embedding column already present")
                else:
                    emit(f"  unexpected ALTER error: {e}")
                    return 1

            # 2. Find rows missing embedding.
            emit("STAGE=find_missing")
            cur.execute(
                f"SELECT chunk_id, tenant_id, chunk_text "
                f"FROM {TABLE} WHERE embedding IS NULL"
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]
            emit(f"  rows_to_embed={len(rows)}")
            if not rows:
                emit("  nothing to backfill")

            # 3. Embed in batches of up to 16 to keep request size bounded.
            BATCH = 16
            for i in range(0, len(rows), BATCH):
                slice_ = rows[i : i + BATCH]
                texts = [r.get("chunk_text") or "" for r in slice_]
                emit(f"STAGE=embed_batch start={i} size={len(texts)}")
                vecs = embed(texts)

                # 4. UPDATE one row at a time. Spark SQL doesn't have a clean
                # multi-row UPDATE in a single statement; per-row keeps the
                # SQL simple and still bound to ~50 rows of one-time backfill.
                emit(f"STAGE=update_batch start={i}")
                for r, vec in zip(slice_, vecs, strict=True):
                    literal = "ARRAY(" + ",".join(
                        f"CAST({v} AS FLOAT)" for v in vec
                    ) + ")"
                    cur.execute(
                        f"""
                        UPDATE {TABLE}
                        SET embedding = {literal}
                        WHERE chunk_id = %(chunk_id)s
                          AND tenant_id = %(tenant_id)s
                        """,
                        {"chunk_id": r["chunk_id"], "tenant_id": r["tenant_id"]},
                    )

            # 5. Verify.
            emit("STAGE=verify")
            cur.execute(
                f"SELECT COUNT(*), COUNT(embedding) FROM {TABLE}"
            )
            total, with_emb = cur.fetchone()
            emit(f"  total_rows={total} with_embedding={with_emb}")

    emit("✅ DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
