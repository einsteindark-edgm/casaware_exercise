"""Force re-vectorize one expense (or a tenant) after a chunk_text fix.

Use when the DLT logic that builds `chunk_text` changes and you need
existing rows in `gold.expense_embeddings` to reflect the new text. The
nominal Temporal `trigger_vector_sync` activity is only invoked at
expense-approval time — already-approved expenses never re-embed unless
forced.

Steps the script does, in order:
  1. (optional) Trigger the gold DLT pipeline so `gold.expense_chunks`
     recomputes its `chunk_text`. Skip with `--no-refresh-pipeline` if
     you've already kicked it off another way.
  2. Wait until the pipeline run reaches a terminal state.
  3. DELETE the embedding row(s) for the target expense(s) in
     `gold.expense_embeddings`.
  4. Re-read the (now updated) chunk_text from `gold.expense_chunks`.
  5. Embed via the Foundation Models endpoint (BGE-large-EN, 1024 dims).
  6. MERGE the new embedding row.

Usage:
    cd nexus-orchestration
    uv run python scripts/force_revectorize_expense.py <expense_id>

    # Multiple at once:
    uv run python scripts/force_revectorize_expense.py exp_A exp_B exp_C

    # Skip pipeline trigger (you already ran it):
    uv run python scripts/force_revectorize_expense.py --no-refresh-pipeline exp_A

    # Whole tenant:
    uv run python scripts/force_revectorize_expense.py --tenant t_alpha
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

env: dict[str, str] = {}
for line in Path(".env").read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    env[k.strip()] = v.strip()

HOST = env["DATABRICKS_HOST"].rstrip("/").replace("https://", "")
TOKEN = env["DATABRICKS_TOKEN"]
WAREHOUSE = env["DATABRICKS_WAREHOUSE_ID"]
CATALOG = env.get("DATABRICKS_CATALOG", "nexus_dev")

os.environ.setdefault("SSL_CERT_FILE", "/etc/ssl/cert.pem")
try:
    CTX = ssl.create_default_context(cafile="/etc/ssl/cert.pem")
except FileNotFoundError:
    CTX = ssl.create_default_context()


def emit(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


# ── Pipeline trigger via REST API ──────────────────────────────────────────


def _api_get(path: str) -> dict:
    h = HOST if HOST.startswith("http") else f"https://{HOST}"
    req = urllib.request.Request(
        f"{h}{path}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    with urllib.request.urlopen(req, timeout=30, context=CTX) as resp:
        return json.loads(resp.read())


def _api_post(path: str, body: dict | None = None) -> dict:
    h = HOST if HOST.startswith("http") else f"https://{HOST}"
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(
        f"{h}{path}",
        data=data,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30, context=CTX) as resp:
        return json.loads(resp.read() or b"{}")


def find_pipeline_id(name_substring: str) -> str | None:
    """Return the pipeline_id whose `name` contains the given substring."""
    out = _api_get("/api/2.0/pipelines?max_results=100")
    for p in out.get("statuses", []) or out.get("pipelines", []):
        n = p.get("name") or ""
        if name_substring.lower() in n.lower():
            return p.get("pipeline_id") or p.get("id")
    return None


def trigger_pipeline_and_wait(pipeline_id: str, max_wait_s: int = 600) -> None:
    emit(f"STAGE=trigger_gold_pipeline id={pipeline_id}")
    update = _api_post(f"/api/2.0/pipelines/{pipeline_id}/updates", {"full_refresh": False})
    update_id = update.get("update_id")
    if not update_id:
        raise RuntimeError(f"no update_id in response: {update}")
    emit(f"  update_id={update_id}")

    deadline = time.time() + max_wait_s
    while time.time() < deadline:
        info = _api_get(f"/api/2.0/pipelines/{pipeline_id}/updates/{update_id}")
        state = info.get("update", {}).get("state")
        emit(f"  state={state}")
        if state in ("COMPLETED",):
            return
        if state in ("FAILED", "CANCELED"):
            raise RuntimeError(f"pipeline update terminal status={state}")
        time.sleep(15)
    raise TimeoutError(f"pipeline did not complete within {max_wait_s}s")


# ── Embedding ──────────────────────────────────────────────────────────────


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


# ── Main re-vectorize ──────────────────────────────────────────────────────


def revectorize(
    expense_ids: list[str] | None,
    tenant: str | None,
    refresh_pipeline: bool,
) -> int:
    from databricks import sql as dbsql  # type: ignore[import-not-found]

    if refresh_pipeline:
        pipeline_id = find_pipeline_id(f"nexus-gold-{CATALOG}")
        if not pipeline_id:
            emit(f"⚠️  could not locate gold pipeline named 'nexus-gold-{CATALOG}'")
            emit("   skipping refresh — re-run with --no-refresh-pipeline if intentional")
            return 2
        trigger_pipeline_and_wait(pipeline_id)

    where_clause: str
    params: dict
    if expense_ids:
        placeholders = ", ".join(f"%(eid_{i})s" for i in range(len(expense_ids)))
        where_clause = f"expense_id IN ({placeholders})"
        params = {f"eid_{i}": eid for i, eid in enumerate(expense_ids)}
    elif tenant:
        where_clause = "tenant_id = %(tenant)s"
        params = {"tenant": tenant}
    else:
        emit("ERROR: must pass either expense_ids or --tenant")
        return 2

    with dbsql.connect(
        server_hostname=HOST,
        http_path=f"/sql/1.0/warehouses/{WAREHOUSE}",
        access_token=TOKEN,
    ) as conn:
        with conn.cursor() as cur:
            emit(f"STAGE=read_chunks where={where_clause}")
            cur.execute(
                f"""
                SELECT chunk_id, tenant_id, expense_id, chunk_text
                FROM {CATALOG}.gold.expense_chunks
                WHERE {where_clause}
                """,
                params,
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]
            emit(f"  found {len(rows)} chunk(s)")
            if not rows:
                return 1

            for r in rows:
                emit(f"  chunk_id={r['chunk_id']} chunk_text=\"{r['chunk_text'][:120]}...\"")

            # Delete then re-embed → MERGE upserts. Cleanest path.
            emit("STAGE=delete_old_embeddings")
            chunk_ids = [r["chunk_id"] for r in rows]
            placeholders = ", ".join(f"%(c_{i})s" for i in range(len(chunk_ids)))
            del_params = {f"c_{i}": cid for i, cid in enumerate(chunk_ids)}
            cur.execute(
                f"""
                DELETE FROM {CATALOG}.gold.expense_embeddings
                WHERE chunk_id IN ({placeholders})
                """,
                del_params,
            )

            emit("STAGE=embed_and_merge")
            BATCH = 16
            for i in range(0, len(rows), BATCH):
                batch = rows[i : i + BATCH]
                texts = [r.get("chunk_text") or "" for r in batch]
                emit(f"  embed batch start={i} size={len(texts)}")
                vecs = embed(texts)
                for r, vec in zip(batch, vecs, strict=True):
                    literal = "ARRAY(" + ",".join(
                        f"CAST({v} AS FLOAT)" for v in vec
                    ) + ")"
                    cur.execute(
                        f"""
                        MERGE INTO {CATALOG}.gold.expense_embeddings t
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
                        {"chunk_id": r["chunk_id"], "tenant_id": r["tenant_id"]},
                    )

    emit("✅ DONE")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("expense_ids", nargs="*", help="One or more expense_ids")
    parser.add_argument(
        "--tenant", help="Re-vectorize ALL chunks for this tenant_id"
    )
    parser.add_argument(
        "--no-refresh-pipeline",
        action="store_true",
        help="Skip the gold DLT pipeline trigger (use if you already refreshed)",
    )
    args = parser.parse_args(argv)

    if not args.expense_ids and not args.tenant:
        parser.error("must pass at least one expense_id or --tenant")

    return revectorize(
        expense_ids=args.expense_ids or None,
        tenant=args.tenant,
        refresh_pipeline=not args.no_refresh_pipeline,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
