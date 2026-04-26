#!/usr/bin/env python3
# ────────────────────────────────────────────────────────────────────────────
# reset-environment.py
#
# Determinista, idempotente. Limpia TODO el stack de Nexus para empezar
# pruebas desde 0 (sin data basura, sin falsos positivos). Lee credenciales:
#   - backend/.env                 (AWS, S3 buckets, REDIS_URL)
#   - nexus-orchestration/.env     (MONGODB_URI, DATABRICKS_*, TEMPORAL_*)
#
# Componentes (flags, todos pueden combinarse):
#   --mongo       Vacia colecciones de MongoDB Atlas (preserva indices).
#   --s3          Borra TODOS los objetos+versiones de los buckets S3.
#   --medallion   DROP bronze/silver/gold tables (auto-detiene bronze DLT).
#   --vector      DELETE el VS index (su storage interno NO se vacia con sync —
#                 hay que borrarlo y recrearlo despues del gold pipeline).
#   --temporal    Termina workflows Running (ExpenseAuditWorkflow zombie post-reset).
#   --all         (default si no se pasa nada) Corre los 5 anteriores.
#
# Nota sobre Redis (ElastiCache en VPC privada, no accesible desde tu Mac):
#   - Backend cachea responses con TTL=60s en `nexus:cache:expense_events:*`.
#   - SSE broker mantiene replay buffers con TTL=3600s en `nexus:events:*:buffer`.
#   - No los limpiamos porque ElastiCache esta en private subnets sin endpoint
#     publico. Para una prueba "desde 0" basta con esperar 1 min (caches
#     expiran) y refrescar el browser (descarta cualquier SSE replay buffer
#     que el cliente pudiera traer).
#
# Modificadores:
#   --dry-run     Imprime que haria sin tocar nada.
#   --yes / -y    No pide confirmacion (para uso en CI/scripts).
#   --skip-cdc-warning  Saltea el auto-stop del bronze pipeline.
#
# Uso tipico:
#   python3 scripts/reset-environment.py            # interactivo, todo
#   python3 scripts/reset-environment.py --mongo --s3 -y
#   python3 scripts/reset-environment.py --dry-run
#
# Despues del reset, para que el sistema siga funcionando recorda:
#   1. Los 3 pipelines DLT (bronze/silver/gold) quedaron STOP — hay que
#      arrancarlos (UI Databricks o esperar el cron de cdc_refresh).
#   2. NO correr setup_vector_search.py completo — esta obsoleto, su celda 1
#      crea expense_chunks como Delta y rompe la MV del gold pipeline.
#   3. El VS index fue BORRADO en el reset (no basta con sync — su storage
#      interno persistia data vieja). Hay que recrearlo con el SDK
#      databricks-vectorsearch DESPUES de que gold rematerialize la source.
#   4. Primera escritura nueva en Mongo deberia llegar a gold en ~2 min.
#
# Requirements: pip install pymongo boto3 requests python-dotenv
# (temporal CLI: brew install temporal)
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

# Mapa de tablas a borrar por schema. La MAYORIA son STs/MVs gestionadas por
# DLT pipelines (bronze_cdc / silver / gold) — DROP TABLE falla con
# "managed by pipeline" si el pipeline esta corriendo, por eso detenemos
# los 3 pipelines antes (ver `_stop_dlt_pipelines_and_wait`). Al re-arrancarlos
# recrean las tablas vacias automaticamente.
#
# Excepcion: gold.expense_embeddings es MANAGED Delta normal — escrita
# append-only por la Temporal activity `sync_vector`. DROP funciona sin
# parar pipelines, pero hay que borrarla para no devolver embeddings viejos
# desde el RAG (falso positivo).
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
        "expense_audit",     # DLT MV gestionada por gold pipeline.
        "expense_chunks",    # DLT MV gestionada por gold pipeline (NO Delta table —
                             # setup_vector_search.py esta obsoleto desde 2026-04-26
                             # cuando se agrego expense_chunks.py al gold pipeline).
        "expense_embeddings",  # MANAGED Delta — append-only INSERT desde sync_vector.
    ],
}

# Pipelines DLT que hay que detener antes de DROP. Match por suffix porque
# en dev el bundle aplica prefix "[dev edgm] ".
DLT_PIPELINE_SUFFIXES = [
    "nexus-bronze-cdc-{catalog}",
    "nexus-silver-{catalog}",
    "nexus-gold-{catalog}",
]

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

    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        err("boto3 no instalado. Corre: pip install boto3")
        return

    # IMPORTANTE: usamos default credential chain (shell env, ~/.aws/credentials,
    # IAM role) en vez de las credenciales del .env. Esas son del usuario IAM
    # del backend (permisos limitados a PutObject sobre prefixes especificos)
    # y devuelven 403 en HeadBucket / DeleteObject. El admin que corre el reset
    # debe tener perms via AWS_PROFILE / aws configure.
    s3 = boto3.client("s3", region_name=region)
    info(f"identidad: {boto3.client('sts').get_caller_identity().get('Arn', '?')}")

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

    try:
        import requests  # noqa: F401  (used by helpers below)
    except ImportError:
        err("requests no instalado. Corre: pip install requests")
        return

    # Casi todas las tablas (excepto gold.expense_embeddings) son STs/MVs
    # gestionadas por los 3 DLT pipelines. DROP TABLE falla con
    # "managed by pipeline" si estan corriendo. Detenemos los 3 antes;
    # al final del script avisamos como reactivarlos.
    pipeline_ids: list[tuple[str, str]] = []  # [(name_suffix, id), ...]
    if not skip_cdc_warning:
        pipeline_ids = _resolve_dlt_pipelines(host, token, catalog)
        for suffix, pid in pipeline_ids:
            info(f"{suffix}: {pid} → stop antes de DROP")
            if dry_run:
                info("(dry-run: no detendria pipeline)")
                continue
            _stop_pipeline_and_wait(host, token, pid)
            ok(f"{suffix} detenido")

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


def _resolve_dlt_pipelines(host: str, token: str, catalog: str) -> list[tuple[str, str]]:
    """Devuelve [(suffix, pipeline_id), ...] para bronze/silver/gold del `catalog`.
    Match por suffix porque el bundle aplica prefix "[dev edgm] " en dev."""
    import requests

    suffixes = [s.format(catalog=catalog) for s in DLT_PIPELINE_SUFFIXES]
    found: dict[str, str] = {}
    url = f"{host}/api/2.0/pipelines"
    next_token: str | None = None
    while True:
        params: dict[str, Any] = {"max_results": 100}
        if next_token:
            params["page_token"] = next_token
        r = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        for p in data.get("statuses", []):
            name = p.get("name", "")
            for s in suffixes:
                if name.endswith(s) and s not in found:
                    found[s] = p.get("pipeline_id")
        next_token = data.get("next_page_token")
        if not next_token:
            break
    missing = [s for s in suffixes if s not in found]
    if missing:
        warn(f"no encontre pipelines: {missing}")
    return [(s, found[s]) for s in suffixes if s in found]


def _stop_pipeline_and_wait(host: str, token: str, pipeline_id: str, timeout: int = 90) -> None:
    """POST /pipelines/{id}/stop y polea hasta state=IDLE."""
    import requests

    requests.post(
        f"{host}/api/2.0/pipelines/{pipeline_id}/stop",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    ).raise_for_status()

    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(
            f"{host}/api/2.0/pipelines/{pipeline_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        r.raise_for_status()
        state = r.json().get("state", "UNKNOWN")
        if state == "IDLE":
            return
        time.sleep(3)
    warn(f"pipeline {pipeline_id} no llego a IDLE en {timeout}s, sigo de todas formas")


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

    try:
        import requests
    except ImportError:
        err("requests no instalado. Corre: pip install requests")
        return

    # Por que DELETE y no SYNC:
    # Un Vector Search Delta-Sync index tiene su PROPIO storage interno
    # (snapshot vectorizado), desacoplado de la source table. Si dropeas la
    # source, los syncs subsecuentes devuelven 404 pero el storage interno
    # del index NO se vacia — los embeddings viejos siguen ahi y un
    # similarity_search los devuelve como falsos positivos.
    #
    # La unica forma limpia de "vaciar" el index es DELETE el recurso entero
    # y recrearlo despues de que el gold pipeline rematerialice la source.
    # No es Delta table (aparece como FOREIGN en information_schema), asi que
    # DROP TABLE no aplica.
    headers = {"Authorization": f"Bearer {token}"}
    get_url = f"{host}/api/2.0/vector-search/indexes/{index}"

    try:
        gr = requests.get(get_url, headers=headers, timeout=30)
    except Exception as e:
        err(f"GET index fallo: {e}")
        return

    if gr.status_code == 404:
        ok("index ya no existe, nada que borrar")
        return
    if gr.status_code != 200:
        err(f"GET index status={gr.status_code}: {gr.text[:200]}")
        return

    indexed_rows = gr.json().get("status", {}).get("indexed_row_count", "?")
    info(f"index existe con {indexed_rows} rows internos → DELETE")

    if dry_run:
        info("(dry-run: no borraria index)")
        return

    try:
        dr = requests.delete(get_url, headers=headers, timeout=30)
        if dr.status_code not in (200, 204):
            err(f"DELETE devolvio status={dr.status_code}: {dr.text[:200]}")
            return
        ok("DELETE OK (puede tardar unos segundos en propagar)")
    except Exception as e:
        err(f"DELETE fallo: {e}")


# ─── component: temporal ────────────────────────────────────────────────────
# Despues del reset cualquier ExpenseAuditWorkflow en estado Running quedaria
# zombie: esperando HITL que nunca llegara, polling tablas Databricks que
# fueron dropeadas, escribiendo a colecciones Mongo vacias. Lo terminamos
# explicitamente para que no contamine la nueva prueba.
def reset_temporal(env: dict[str, str], dry_run: bool) -> None:
    header("Temporal (Running workflows)")
    host = env.get("TEMPORAL_HOST", "localhost:7233")
    namespace = env.get("TEMPORAL_NAMESPACE", "default")
    info(f"host: {host}  namespace: {namespace}")

    # Usamos el CLI `temporal` para evitar dependencias pesadas (temporalio).
    import shutil
    import subprocess

    if not shutil.which("temporal"):
        err("CLI 'temporal' no encontrado en PATH. brew install temporal")
        return

    query = (
        "ExecutionStatus='Running' AND "
        "(WorkflowType='ExpenseAuditWorkflow' OR WorkflowType='RagQueryWorkflow')"
    )
    list_cmd = [
        "temporal", "workflow", "list",
        "--address", host,
        "--namespace", namespace,
        "--query", query,
        "--limit", "200",
        "--output", "json",
    ]
    try:
        out = subprocess.run(list_cmd, capture_output=True, text=True, timeout=15, check=True)
    except subprocess.CalledProcessError as e:
        err(f"temporal list fallo: {e.stderr.strip() or e.stdout.strip()}")
        return
    except FileNotFoundError:
        err("temporal CLI no encontrado")
        return

    import json as _json

    raw = out.stdout.strip()
    if not raw:
        ok("0 workflows Running, nada que terminar")
        return

    # `temporal workflow list -o json` emite un array JSON.
    try:
        items = _json.loads(raw)
    except _json.JSONDecodeError:
        # Fallback: jsonl (una linea por workflow)
        items = [_json.loads(l) for l in raw.splitlines() if l.strip()]

    if not items:
        ok("0 workflows Running")
        return

    info(f"encontre {len(items)} workflows Running")
    for w in items:
        wf_id = w.get("execution", {}).get("workflowId") or w.get("workflow_id")
        if not wf_id:
            continue
        if dry_run:
            info(f"{wf_id}: terminaria (dry-run)")
            continue
        term_cmd = [
            "temporal", "workflow", "terminate",
            "--address", host,
            "--namespace", namespace,
            "--workflow-id", wf_id,
            "--reason", "reset-environment.py",
        ]
        try:
            subprocess.run(term_cmd, capture_output=True, text=True, timeout=15, check=True)
            ok(f"{wf_id}: terminado")
        except subprocess.CalledProcessError as e:
            err(f"{wf_id}: {e.stderr.strip() or e.stdout.strip()}")


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
    if "temporal" in components:
        print(f"  • Temporal:   terminate Running workflows en {env.get('TEMPORAL_HOST', 'localhost:7233')}")
    print(f"{C.R}{'─' * 70}{C.OFF}")
    resp = input("\nEscribe 'RESET' para continuar (cualquier otra cosa cancela): ").strip()
    return resp == "RESET"


def main() -> int:
    p = argparse.ArgumentParser(description="Reset Nexus dev environment to a clean state.")
    p.add_argument("--mongo", action="store_true", help="Vacia colecciones MongoDB")
    p.add_argument("--s3", action="store_true", help="Borra objetos S3")
    p.add_argument("--medallion", action="store_true", help="DROP tablas bronze/silver/gold")
    p.add_argument("--vector", action="store_true", help="Sync index Vector Search")
    p.add_argument("--temporal", action="store_true", help="Termina workflows Running")
    p.add_argument("--all", action="store_true", help="Todos los componentes (default)")
    p.add_argument("--dry-run", action="store_true", help="No toca nada, solo imprime")
    p.add_argument("-y", "--yes", action="store_true", help="No pide confirmacion")
    p.add_argument(
        "--skip-cdc-warning",
        action="store_true",
        help="No detener bronze pipeline antes de DROP (asume ya detenido)",
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
    if args.temporal:
        components.append("temporal")

    # Default: --all si no se paso ningun flag de componente.
    # Orden importa: temporal primero (no dejar activities en vuelo apuntando
    # a datos a punto de borrarse), despues mongo/s3/medallion, vector al
    # final (sync del index VS se beneficia de gold ya vacio).
    if not components or args.all:
        components = ["temporal", "mongo", "s3", "medallion", "vector"]

    env = load_env_files()
    print(f"{C.DIM}cargado .env de: {[str(f) for f in ENV_FILES]}{C.OFF}")
    print(f"{C.DIM}componentes: {components}{C.OFF}")
    if args.dry_run:
        print(f"{C.Y}DRY-RUN — no se ejecuta nada destructivo{C.OFF}")

    if not args.dry_run and not args.yes:
        if not confirm(env, components):
            print("cancelado.")
            return 1

    # Orden de ejecucion: temporal primero (no dejar activities en vuelo
    # apuntando a datos a punto de borrarse), luego datos, luego caches.
    if "temporal" in components:
        reset_temporal(env, args.dry_run)
    if "mongo" in components:
        reset_mongo(env, args.dry_run)
    if "s3" in components:
        reset_s3(env, args.dry_run)
    if "medallion" in components:
        reset_medallion(env, args.dry_run, args.skip_cdc_warning)
    if "vector" in components:
        reset_vector(env, args.dry_run)

    print(f"\n{C.G}listo.{C.OFF}")
    if not args.dry_run:
        print(
            f"\n{C.Y}Recordatorio post-reset:{C.OFF}\n"
            "  1. Los 3 pipelines DLT (bronze/silver/gold) quedaron STOP.\n"
            "     - Bronze: arrancalo desde la UI de Databricks (volvera a\n"
            "       continuous=true) o esperá al proximo GHA deploy.\n"
            "     - Silver y Gold: el job 'nexus-cdc-refresh' (cron 1 min)\n"
            "       los dispara automaticamente; tambien podes arrancarlos\n"
            "       a mano. Al arrancar recrean las MVs/STs vacias.\n"
            "  2. NO correr el notebook completo setup_vector_search.py — esta\n"
            "     obsoleto. Su celda 1 crea expense_chunks como Delta table y\n"
            "     romperia la MV gestionada por el gold pipeline.\n"
            "  3. El VS index fue BORRADO (su storage interno tenia rows viejos\n"
            "     que un sync no podia limpiar). Hay que RECREARLO despues de\n"
            "     que el gold pipeline rematerialice gold.expense_chunks.\n"
            "     Receta SDK (4 lineas en un notebook):\n"
            "       from databricks.vector_search.client import VectorSearchClient\n"
            "       VectorSearchClient().create_delta_sync_index(\n"
            "         endpoint_name='nexus-vs-dev',\n"
            "         source_table_name='nexus_dev.gold.expense_chunks',\n"
            "         index_name='nexus_dev.vector.expense_chunks_index',\n"
            "         primary_key='chunk_id', pipeline_type='TRIGGERED',\n"
            "         embedding_source_column='chunk_text',\n"
            "         embedding_model_endpoint_name='databricks-bge-large-en')\n"
            "     (o las celdas 2 y 3 SOLAS de setup_vector_search.py — NO la 1)\n"
            "  4. Redis (ElastiCache, no tocado): cache TTL=60s, SSE buffers TTL=1h.\n"
            "     Espera ~1 min antes de probar y refresca el browser para descartar\n"
            "     cualquier replay buffer del cliente.\n"
            "  5. Primer expense nuevo deberia llegar a gold en ~2 min."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
