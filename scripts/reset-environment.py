#!/usr/bin/env python3
# ────────────────────────────────────────────────────────────────────────────
# reset-environment.py
#
# Determinista, idempotente. Limpia todo el stack de Nexus para empezar
# pruebas desde 0 (sin data basura). Lee credenciales de:
#   - backend/.env                 (AWS, S3 buckets)
#   - nexus-orchestration/.env     (MONGODB_URI, DATABRICKS_*)
#
# Componentes (flags, todos pueden combinarse):
#   --mongo       Vacia colecciones de MongoDB Atlas (preserva indices).
#   --s3          Borra TODOS los objetos+versiones de los buckets S3.
#   --medallion   TRUNCATE bronze/silver/gold tables en Databricks UC.
#   --vector      Vacia gold.expense_chunks y dispara sync del index VS.
#   --all         (default si no se pasa nada) Corre los 4 anteriores.
#
# Modificadores:
#   --dry-run     Imprime que haria sin tocar nada.
#   --yes / -y    No pide confirmacion (para uso en CI/scripts).
#   --skip-cdc-warning  Asume que el pipeline DLT bronze esta detenido.
#
# Uso tipico:
#   python3 scripts/reset-environment.py            # interactivo, todo
#   python3 scripts/reset-environment.py --mongo --s3 -y
#   python3 scripts/reset-environment.py --dry-run
#
# Requirements: pip install pymongo boto3 requests python-dotenv
# ────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILES = [
    REPO_ROOT / "backend" / ".env",
    REPO_ROOT / "nexus-orchestration" / ".env",
]

MONGO_COLLECTIONS = [
    "expenses",
    "receipts",
    "expense_events",
    "hitl_tasks",
    "ocr_extractions",
    "chat_sessions",
    "chat_turns",
]

# Tablas Delta-managed (no DLT). DROP IF EXISTS las recrea el siguiente run
# de los pipelines DLT. Para bronze.mongodb_cdc_*: si el pipeline esta corriendo,
# DROP fallara con "table is being managed by pipeline" — detener primero.
MEDALLION_TABLES: dict[str, list[str]] = {
    "bronze": [
        "mongodb_cdc_expenses",
        "mongodb_cdc_receipts",
        "mongodb_cdc_hitl_tasks",
        "mongodb_cdc_ocr_extractions",
        "mongodb_cdc_expense_events",
    ],
    "silver": [
        "expenses",
        "ocr_extractions",
        "expense_events",
        "hitl_events",
        "hitl_discrepancies",
    ],
    "gold": [
        "expense_audit",
        "expense_chunks",  # Delta TABLE (no MV) — re-creada por setup_vector_search.py
    ],
}

VECTOR_ENDPOINT = "nexus-vs-dev"
VECTOR_INDEX = "nexus_dev.vector.expense_chunks_index"
VECTOR_SOURCE_TABLE = "nexus_dev.gold.expense_chunks"


# ─── env loading ────────────────────────────────────────────────────────────
def load_env_files() -> dict[str, str]:
    env: dict[str, str] = {}
    for f in ENV_FILES:
        if not f.exists():
            print(f"[warn] {f} no existe, ignorando", file=sys.stderr)
            continue
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            # nexus-orchestration/.env tiene MONGODB_URI prod (Atlas).
            # Esa gana sobre la de backend/.env (mongo://mongo:27017 local).
            env[k] = v
    return env


# ─── pretty printing ────────────────────────────────────────────────────────
class C:
    R = "\033[31m"
    G = "\033[32m"
    Y = "\033[33m"
    B = "\033[34m"
    M = "\033[35m"
    DIM = "\033[2m"
    OFF = "\033[0m"


def header(label: str) -> None:
    print(f"\n{C.B}═══ {label} ═══{C.OFF}")


def info(msg: str) -> None:
    print(f"  {msg}")


def ok(msg: str) -> None:
    print(f"  {C.G}✓{C.OFF} {msg}")


def warn(msg: str) -> None:
    print(f"  {C.Y}⚠{C.OFF} {msg}")


def err(msg: str) -> None:
    print(f"  {C.R}✗{C.OFF} {msg}", file=sys.stderr)


# ─── component: mongodb ─────────────────────────────────────────────────────
def reset_mongo(env: dict[str, str], dry_run: bool) -> None:
    header("MongoDB Atlas")
    uri = env.get("MONGODB_URI")
    db_name = env.get("MONGODB_DB", "nexus_dev")
    if not uri:
        err("MONGODB_URI no encontrada en .env")
        return

    safe_uri = uri.split("@")[-1] if "@" in uri else uri
    info(f"db: {db_name} @ {safe_uri}")

    try:
        from pymongo import MongoClient
    except ImportError:
        err("pymongo no instalado. Corre: pip install pymongo")
        return

    client = MongoClient(uri, serverSelectionTimeoutMS=10000)
    try:
        client.admin.command("ping")
    except Exception as e:
        err(f"no se pudo conectar a MongoDB: {e}")
        return

    db = client[db_name]
    existing = set(db.list_collection_names())

    for coll_name in MONGO_COLLECTIONS:
        if coll_name not in existing:
            info(f"{coll_name}: no existe, skip")
            continue
        count = db[coll_name].estimated_document_count()
        if dry_run:
            info(f"{coll_name}: borraria {count} docs (dry-run)")
            continue
        if count == 0:
            ok(f"{coll_name}: ya vacia")
            continue
        result = db[coll_name].delete_many({})
        ok(f"{coll_name}: {result.deleted_count} docs borrados")

    client.close()


# ─── component: s3 ──────────────────────────────────────────────────────────
def reset_s3(env: dict[str, str], dry_run: bool) -> None:
    header("S3 Buckets")
    buckets = [
        env.get("S3_RECEIPTS_BUCKET", "nexus-dev-edgm-receipts"),
        env.get("S3_TEXTRACT_OUTPUT_BUCKET", "nexus-dev-edgm-textract-output"),
    ]
    region = env.get("AWS_REGION", "us-east-1")
    access_key = env.get("AWS_ACCESS_KEY_ID")
    secret_key = env.get("AWS_SECRET_ACCESS_KEY")

    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        err("boto3 no instalado. Corre: pip install boto3")
        return

    session_kwargs: dict[str, Any] = {"region_name": region}
    if access_key and secret_key:
        session_kwargs["aws_access_key_id"] = access_key
        session_kwargs["aws_secret_access_key"] = secret_key

    s3 = boto3.client("s3", **session_kwargs)

    for bucket in buckets:
        info(f"bucket: {bucket}")
        try:
            s3.head_bucket(Bucket=bucket)
        except ClientError as e:
            err(f"  no accesible ({e.response['Error']['Code']}), skip")
            continue

        # Borrar objetos current
        deleted = _delete_all_objects(s3, bucket, dry_run)
        # Borrar versiones (receipts tiene versioning enabled)
        deleted_versions = _delete_all_versions(s3, bucket, dry_run)

        verb = "borraria" if dry_run else "borrados"
        ok(f"  {deleted} objetos + {deleted_versions} versiones {verb}")


def _delete_all_objects(s3, bucket: str, dry_run: bool) -> int:
    total = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if not keys:
            continue
        total += len(keys)
        if not dry_run:
            for i in range(0, len(keys), 1000):
                s3.delete_objects(Bucket=bucket, Delete={"Objects": keys[i : i + 1000]})
    return total


def _delete_all_versions(s3, bucket: str, dry_run: bool) -> int:
    total = 0
    paginator = s3.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket):
        items: list[dict[str, str]] = []
        for v in page.get("Versions", []):
            items.append({"Key": v["Key"], "VersionId": v["VersionId"]})
        for m in page.get("DeleteMarkers", []):
            items.append({"Key": m["Key"], "VersionId": m["VersionId"]})
        if not items:
            continue
        total += len(items)
        if not dry_run:
            for i in range(0, len(items), 1000):
                s3.delete_objects(Bucket=bucket, Delete={"Objects": items[i : i + 1000]})
    return total


# ─── component: medallion (Databricks UC) ───────────────────────────────────
def reset_medallion(env: dict[str, str], dry_run: bool, skip_cdc_warning: bool) -> None:
    header("Medallion (Databricks Unity Catalog)")
    host = env.get("DATABRICKS_HOST", "").rstrip("/")
    token = env.get("DATABRICKS_TOKEN")
    warehouse_id = env.get("DATABRICKS_WAREHOUSE_ID")
    catalog = env.get("DATABRICKS_CATALOG", "nexus_dev")

    if not (host and token and warehouse_id):
        err("DATABRICKS_HOST / DATABRICKS_TOKEN / DATABRICKS_WAREHOUSE_ID faltan")
        return

    info(f"host: {host}")
    info(f"warehouse: {warehouse_id}")
    info(f"catalog: {catalog}")

    if not skip_cdc_warning:
        warn(
            "bronze.mongodb_cdc_* las maneja el DLT pipeline 'bronze_cdc_pipeline'.\n"
            "    Si el pipeline esta corriendo, DROP fallara con 'managed by pipeline'.\n"
            "    Detén el pipeline primero (UI Databricks → Workflows → Pipelines)\n"
            "    o pasa --skip-cdc-warning si ya esta detenido."
        )

    try:
        import requests
    except ImportError:
        err("requests no instalado. Corre: pip install requests")
        return

    statements: list[tuple[str, str]] = []
    for schema, tables in MEDALLION_TABLES.items():
        for t in tables:
            fq = f"{catalog}.{schema}.{t}"
            statements.append((fq, f"DROP TABLE IF EXISTS {fq}"))

    for fq, sql in statements:
        if dry_run:
            info(f"{fq}: ejecutaria '{sql}' (dry-run)")
            continue
        try:
            _exec_sql(host, token, warehouse_id, sql, catalog)
            ok(f"{fq}: drop OK")
        except Exception as e:
            err(f"{fq}: {e}")


def _exec_sql(
    host: str, token: str, warehouse_id: str, sql: str, catalog: str, timeout: int = 60
) -> dict[str, Any]:
    """Statement Execution API — sincronía con polling."""
    import requests

    url = f"{host}/api/2.0/sql/statements"
    payload = {
        "warehouse_id": warehouse_id,
        "statement": sql,
        "catalog": catalog,
        "wait_timeout": "30s",
        "on_wait_timeout": "CONTINUE",
    }
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    statement_id = data["statement_id"]
    state = data.get("status", {}).get("state", "PENDING")

    deadline = time.time() + timeout
    while state in ("PENDING", "RUNNING") and time.time() < deadline:
        time.sleep(1.5)
        rr = requests.get(
            f"{url}/{statement_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
        rr.raise_for_status()
        data = rr.json()
        state = data.get("status", {}).get("state", "PENDING")

    if state != "SUCCEEDED":
        msg = data.get("status", {}).get("error", {}).get("message", state)
        raise RuntimeError(f"{state}: {msg}")
    return data


# ─── component: vector search ───────────────────────────────────────────────
def reset_vector(env: dict[str, str], dry_run: bool) -> None:
    header("Vector Search (Mosaic AI)")
    host = env.get("DATABRICKS_HOST", "").rstrip("/")
    token = env.get("DATABRICKS_TOKEN")
    endpoint = env.get("DATABRICKS_VS_ENDPOINT", VECTOR_ENDPOINT)
    index = env.get("DATABRICKS_VS_INDEX", VECTOR_INDEX)

    if not (host and token):
        err("DATABRICKS_HOST / DATABRICKS_TOKEN faltan")
        return

    info(f"endpoint: {endpoint}")
    info(f"index:    {index}")
    info("(la tabla source gold.expense_chunks se borra en --medallion)")

    if dry_run:
        info("dispararia sync del index (dry-run)")
        return

    try:
        import requests
    except ImportError:
        err("requests no instalado. Corre: pip install requests")
        return

    # POST /api/2.0/vector-search/indexes/{index}/sync
    url = f"{host}/api/2.0/vector-search/indexes/{index}/sync"
    try:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if r.status_code == 404:
            warn("index no existe — corre setup_vector_search.py para crearlo")
            return
        r.raise_for_status()
        ok("sync disparado (puede tardar minutos en propagar el truncate)")
    except Exception as e:
        err(f"sync fallo: {e}")


# ─── orchestrator ───────────────────────────────────────────────────────────
def confirm(env: dict[str, str], components: list[str]) -> bool:
    safe_uri = "?"
    if uri := env.get("MONGODB_URI"):
        safe_uri = uri.split("@")[-1] if "@" in uri else uri
    print(f"\n{C.R}{'─' * 70}{C.OFF}")
    print(f"{C.R}OPERACION DESTRUCTIVA — vas a borrar:{C.OFF}")
    print(f"{C.R}{'─' * 70}{C.OFF}")
    if "mongo" in components:
        print(f"  • MongoDB:    {safe_uri} ({env.get('MONGODB_DB', 'nexus_dev')})")
        print(f"               colecciones: {', '.join(MONGO_COLLECTIONS)}")
    if "s3" in components:
        print(f"  • S3:         {env.get('S3_RECEIPTS_BUCKET')}")
        print(f"               {env.get('S3_TEXTRACT_OUTPUT_BUCKET')}")
    if "medallion" in components:
        catalog = env.get("DATABRICKS_CATALOG", "nexus_dev")
        all_tables = sum(len(v) for v in MEDALLION_TABLES.values())
        print(f"  • Databricks: {catalog} catalog, {all_tables} tablas (bronze/silver/gold)")
    if "vector" in components:
        print(f"  • Vector:     sync de {VECTOR_INDEX}")
    print(f"{C.R}{'─' * 70}{C.OFF}")
    resp = input(f"\nEscribe 'RESET' para continuar (cualquier otra cosa cancela): ").strip()
    return resp == "RESET"


def main() -> int:
    p = argparse.ArgumentParser(description="Reset Nexus dev environment to a clean state.")
    p.add_argument("--mongo", action="store_true", help="Vacia colecciones MongoDB")
    p.add_argument("--s3", action="store_true", help="Borra objetos S3")
    p.add_argument("--medallion", action="store_true", help="DROP tablas bronze/silver/gold")
    p.add_argument("--vector", action="store_true", help="Sync index Vector Search")
    p.add_argument("--all", action="store_true", help="Todos los componentes (default)")
    p.add_argument("--dry-run", action="store_true", help="No toca nada, solo imprime")
    p.add_argument("-y", "--yes", action="store_true", help="No pide confirmacion")
    p.add_argument(
        "--skip-cdc-warning",
        action="store_true",
        help="Asume que el DLT pipeline bronze esta detenido",
    )
    args = p.parse_args()

    components: list[str] = []
    if args.mongo:
        components.append("mongo")
    if args.s3:
        components.append("s3")
    if args.medallion:
        components.append("medallion")
    if args.vector:
        components.append("vector")

    # Default: --all si no se paso ningun flag de componente
    if not components or args.all:
        components = ["mongo", "s3", "medallion", "vector"]

    env = load_env_files()
    print(f"{C.DIM}cargado .env de: {[str(f) for f in ENV_FILES]}{C.OFF}")
    print(f"{C.DIM}componentes: {components}{C.OFF}")
    if args.dry_run:
        print(f"{C.Y}DRY-RUN — no se ejecuta nada destructivo{C.OFF}")

    if not args.dry_run and not args.yes:
        if not confirm(env, components):
            print("cancelado.")
            return 1

    if "mongo" in components:
        reset_mongo(env, args.dry_run)
    if "s3" in components:
        reset_s3(env, args.dry_run)
    if "medallion" in components:
        reset_medallion(env, args.dry_run, args.skip_cdc_warning)
    if "vector" in components:
        reset_vector(env, args.dry_run)

    print(f"\n{C.G}listo.{C.OFF}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
