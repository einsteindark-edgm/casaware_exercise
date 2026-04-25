"""Print, en limpio, el contenido vectorizado de un expense_id.

Muestra exactamente lo que ve el agente AI cuando hace búsqueda semántica:
  - El `chunk_text` que se embedió (texto plano que va al modelo de embeddings).
  - Los metadatos filtrables (tenant, vendor, amount, date, category).
  - Si existe (y tiene) embedding en `gold.expense_embeddings`, su dimensión
    y un preview de los primeros 8 floats — sirve para confirmar que el
    vector existe sin volcar 1024 floats al stdout.
  - Como contexto: el `ocr_extra` crudo de `gold.expense_audit` (JSON original
    con line_items + summary_fields), para comparar entrada vs. chunk.

Uso:
    cd nexus-orchestration
    uv run python scripts/inspect_vectorized_expense.py exp_01KQ3BAQ6PP5YME7WH53WJQV4X
"""
from __future__ import annotations

import json
import os
import sys
import time
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


def hr(title: str) -> None:
    print(f"\n{'─' * 70}")
    print(f"  {title}")
    print("─" * 70)


def main(expense_id: str) -> int:
    from databricks import sql as dbsql

    with dbsql.connect(
        server_hostname=HOST,
        http_path=f"/sql/1.0/warehouses/{WAREHOUSE}",
        access_token=TOKEN,
    ) as conn:
        with conn.cursor() as cur:
            # 1. gold.expense_chunks — exactamente lo que se embebe.
            hr(f"gold.expense_chunks  (expense_id = {expense_id})")
            cur.execute(
                f"""
                SELECT chunk_id, tenant_id, chunk_text,
                       vendor, amount, currency, date, category, approved_at
                FROM {CATALOG}.gold.expense_chunks
                WHERE expense_id = %(eid)s
                """,
                {"eid": expense_id},
            )
            rows = cur.fetchall()
            if not rows:
                print(f"  (no chunk en gold.expense_chunks para {expense_id})")
                print("  Posibles razones: el expense aún no ha sido aprobado,")
                print("  o el pipeline DLT gold no se ha ejecutado todavía.")
                return 1

            for r in rows:
                chunk_id, tenant_id, chunk_text, vendor, amount, currency, date, category, approved_at = r
                print(f"  chunk_id     = {chunk_id}")
                print(f"  tenant_id    = {tenant_id}")
                print(f"  vendor       = {vendor}")
                print(f"  amount       = {amount} {currency}")
                print(f"  date         = {date}")
                print(f"  category     = {category}")
                print(f"  approved_at  = {approved_at}")
                print()
                print("  ▸ chunk_text (esto es lo que ve el modelo de embeddings):")
                print()
                # Indentar cada línea para distinguirlo del metadata.
                for line in (chunk_text or "").splitlines() or [chunk_text or ""]:
                    print(f"      {line}")

            # 2. gold.expense_embeddings — el vector real.
            hr("gold.expense_embeddings  (vector real)")
            cur.execute(
                f"""
                SELECT chunk_id, tenant_id,
                       size(embedding) AS dim,
                       slice(embedding, 1, 8) AS preview,
                       updated_at
                FROM {CATALOG}.gold.expense_embeddings
                WHERE chunk_id IN (
                    SELECT chunk_id FROM {CATALOG}.gold.expense_chunks
                    WHERE expense_id = %(eid)s
                )
                """,
                {"eid": expense_id},
            )
            emb_rows = cur.fetchall()
            if not emb_rows:
                print("  (sin embedding aún en gold.expense_embeddings)")
                print("  La activity trigger_vector_sync todavía no ha corrido")
                print("  para este chunk — o falló. Revisa Temporal UI.")
            else:
                for r in emb_rows:
                    chunk_id, tenant_id, dim, preview, updated_at = r
                    print(f"  chunk_id    = {chunk_id}")
                    print(f"  dim         = {dim}  (modelo: databricks-bge-large-en → 1024)")
                    print(f"  updated_at  = {updated_at}")
                    print(f"  preview[:8] = {preview}")

            # 3. Contexto: ocr_extra crudo desde gold.expense_audit.
            hr("gold.expense_audit  (entrada cruda — ocr_extra JSON)")
            cur.execute(
                f"""
                SELECT ocr_extra
                FROM {CATALOG}.gold.expense_audit
                WHERE expense_id = %(eid)s
                """,
                {"eid": expense_id},
            )
            audit = cur.fetchone()
            if not audit or not audit[0]:
                print("  (sin ocr_extra; el receipt no fue procesado por Textract")
                print("   o fue ingerido antes de Phase A.1)")
            else:
                try:
                    parsed = json.loads(audit[0])
                    print(json.dumps(parsed, indent=2, ensure_ascii=False))
                except (TypeError, json.JSONDecodeError):
                    print(audit[0])

    print()
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("uso: python inspect_vectorized_expense.py <expense_id>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
