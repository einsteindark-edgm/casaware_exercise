#!/usr/bin/env python3
# ────────────────────────────────────────────────────────────────────────────
# recreate-vs-index.py
#
# Recrea el Mosaic AI Vector Search index `nexus_dev.vector.expense_chunks_index`
# despues de un reset-environment.py. Necesario porque ese reset BORRA el
# index entero (su storage interno persiste data vieja aunque la source este
# vacia — la unica forma de "vaciarlo" es DELETE + recreate).
#
# Cuando correrlo:
#   - Despues de scripts/reset-environment.py
#   - Despues de que el gold pipeline haya rematerializado gold.expense_chunks
#     (chequear en Databricks UI o esperar al primer expense aprobado que
#     llegue a gold ~2min despues del reset)
#
# Idempotente:
#   - Si el index ya existe, no hace nada (sale 0).
#   - Si la source table no existe o esta vacia, sale con error y te dice
#     que esperes al gold pipeline.
#
# Uso:
#   python3 scripts/recreate-vs-index.py
#   python3 scripts/recreate-vs-index.py --wait    # poll hasta que la source exista
#
# Requirements: pip install requests
# ────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / "nexus-orchestration" / ".env"

ENDPOINT_NAME = "nexus-vs-dev"
INDEX_NAME = "nexus_dev.vector.expense_chunks_index"
SOURCE_TABLE = "nexus_dev.gold.expense_chunks"
PRIMARY_KEY = "chunk_id"
EMBEDDING_COL = "chunk_text"
EMBEDDING_MODEL = "databricks-bge-large-en"


class C:
    R = "\033[31m"
    G = "\033[32m"
    Y = "\033[33m"
    B = "\033[34m"
    DIM = "\033[2m"
    OFF = "\033[0m"


def info(m: str) -> None:
    print(f"  {m}")


def ok(m: str) -> None:
    print(f"  {C.G}✓{C.OFF} {m}")


def warn(m: str) -> None:
    print(f"  {C.Y}⚠{C.OFF} {m}")


def err(m: str) -> None:
    print(f"  {C.R}✗{C.OFF} {m}", file=sys.stderr)


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if not ENV_FILE.exists():
        err(f"{ENV_FILE} no existe")
        sys.exit(2)
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def source_table_status(host: str, token: str, warehouse_id: str) -> tuple[bool, int]:
    """Returns (exists, row_count). row_count = -1 if not exists."""
    import requests

    url = f"{host}/api/2.0/sql/statements"
    payload = {
        "warehouse_id": warehouse_id,
        "statement": f"SELECT COUNT(*) FROM {SOURCE_TABLE}",
        "wait_timeout": "30s",
        "on_wait_timeout": "CONTINUE",
    }
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    state = data.get("status", {}).get("state", "PENDING")
    statement_id = data["statement_id"]

    deadline = time.time() + 60
    while state in ("PENDING", "RUNNING") and time.time() < deadline:
        time.sleep(1.5)
        rr = requests.get(
            f"{url}/{statement_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        )
        rr.raise_for_status()
        data = rr.json()
        state = data.get("status", {}).get("state", "PENDING")

    if state != "SUCCEEDED":
        msg = data.get("status", {}).get("error", {}).get("message", state)
        if "TABLE_OR_VIEW_NOT_FOUND" in msg or "does not exist" in msg.lower():
            return (False, -1)
        raise RuntimeError(f"{state}: {msg}")
    rows = data.get("result", {}).get("data_array", [])
    count = int(rows[0][0]) if rows else 0
    return (True, count)


def index_exists(host: str, token: str) -> bool:
    import requests

    r = requests.get(
        f"{host}/api/2.0/vector-search/indexes/{INDEX_NAME}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    return r.status_code == 200


def create_index(host: str, token: str) -> None:
    import requests

    payload = {
        "name": INDEX_NAME,
        "endpoint_name": ENDPOINT_NAME,
        "primary_key": PRIMARY_KEY,
        "index_type": "DELTA_SYNC",
        "delta_sync_index_spec": {
            "source_table": SOURCE_TABLE,
            "pipeline_type": "TRIGGERED",
            "embedding_source_columns": [
                {
                    "name": EMBEDDING_COL,
                    "embedding_model_endpoint_name": EMBEDDING_MODEL,
                }
            ],
        },
    }
    r = requests.post(
        f"{host}/api/2.0/vector-search/indexes",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=60,
    )
    if r.status_code >= 400:
        err(f"create devolvio {r.status_code}: {r.text[:400]}")
        sys.exit(3)


def wait_until_ready(host: str, token: str, timeout: int = 600) -> None:
    """Polea hasta que el index este READY (primera sync puede tardar ~5min)."""
    import requests

    deadline = time.time() + timeout
    last_state = "?"
    while time.time() < deadline:
        r = requests.get(
            f"{host}/api/2.0/vector-search/indexes/{INDEX_NAME}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if r.status_code == 200:
            status = r.json().get("status", {})
            ready = status.get("ready", False)
            detailed = status.get("detailed_state", "?")
            if detailed != last_state:
                info(f"detailed_state={detailed} ready={ready}")
                last_state = detailed
            if ready:
                ok(f"index READY (rows={status.get('indexed_row_count', '?')})")
                return
        time.sleep(15)
    warn(f"timeout {timeout}s — el index sigue construyendose, chequea en la UI")


def main() -> int:
    p = argparse.ArgumentParser(description="Recrea el VS index post-reset.")
    p.add_argument(
        "--wait",
        action="store_true",
        help="Si la source no existe, polea cada 30s (timeout 10min)",
    )
    p.add_argument(
        "--no-wait-ready",
        action="store_true",
        help="No esperar a que el index quede READY (sale tras crear)",
    )
    args = p.parse_args()

    env = load_env()
    host = env.get("DATABRICKS_HOST", "").rstrip("/")
    token = env.get("DATABRICKS_TOKEN", "")
    warehouse_id = env.get("DATABRICKS_WAREHOUSE_ID", "")

    if not (host and token and warehouse_id):
        err("DATABRICKS_HOST / DATABRICKS_TOKEN / DATABRICKS_WAREHOUSE_ID faltan en .env")
        return 2

    print(f"{C.B}═══ Vector Search index recreate ═══{C.OFF}")
    info(f"host:  {host}")
    info(f"index: {INDEX_NAME}")

    if index_exists(host, token):
        ok("index ya existe — nada que hacer (idempotente)")
        return 0

    # Verificar source
    deadline = time.time() + (600 if args.wait else 0)
    while True:
        try:
            exists, count = source_table_status(host, token, warehouse_id)
        except Exception as e:
            err(f"chequeo source fallo: {e}")
            return 4

        if exists and count > 0:
            ok(f"source {SOURCE_TABLE} OK (rows={count})")
            break
        if exists and count == 0:
            err(f"source {SOURCE_TABLE} existe pero esta vacia — disparar gold pipeline primero")
            return 5
        # No existe
        if not args.wait or time.time() >= deadline:
            err(
                f"source {SOURCE_TABLE} no existe.\n"
                "    Esperá a que el gold pipeline corra y materialice la MV,\n"
                "    o pasa --wait para que este script polee hasta que aparezca."
            )
            return 5
        info(f"source aun no existe, esperando 30s... (queda {int(deadline - time.time())}s)")
        time.sleep(30)

    info(f"creando index {INDEX_NAME} → endpoint {ENDPOINT_NAME}")
    create_index(host, token)
    ok("create OK, esperando READY...")

    if args.no_wait_ready:
        info("--no-wait-ready: salgo sin esperar a READY")
        return 0

    wait_until_ready(host, token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
