# Phase D — Runbook (real-mode standup)

Este runbook asume que el código D.1–D.11 ya está en main y que el preflight
del worker (`nexus-rag-tq`) rechaza arrancar si faltan credenciales.

## 0. Pre-requisitos

- **AWS**: rol/IAM con permiso `bedrock:InvokeModel` y `bedrock:InvokeModelWithResponseStream`
  sobre `anthropic.claude-sonnet-4-6-*`. Credenciales vía env o task role (ECS).
- **Databricks**: workspace con Unity Catalog, un SQL warehouse, y feature
  "Mosaic AI Vector Search" habilitado.
- **Redis** + **Mongo** + **Temporal** ya existentes (los usa el resto del stack).

## 1. Credenciales en `.env`

Editar `nexus-orchestration/.env`:

```
FAKE_PROVIDERS=false

DATABRICKS_HOST=https://dbc-xxxxxxxx-xxxx.cloud.databricks.com
DATABRICKS_TOKEN=dapi...
DATABRICKS_WAREHOUSE_ID=0123456789abcdef
DATABRICKS_CATALOG=nexus_dev
DATABRICKS_VS_ENDPOINT=nexus-vs-dev
DATABRICKS_VS_INDEX=nexus_dev.vector.expense_chunks_index

BEDROCK_MODEL_ID=anthropic.claude-sonnet-4-6-20250514-v1:0
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
```

El preflight del worker exige todas estas llaves cuando `FAKE_PROVIDERS=false`.

## 2. Bronze → Silver → Gold (una sola vez)

El medallion ya está versionado en `nexus-medallion/src/`. Se despliega como
Lakeflow Declarative Pipeline. Si aún no hay filas en `gold.expense_audit`,
aprobar algunos expenses vía backend para que Lakeflow los promueva.

Validar en SQL warehouse:

```sql
SELECT COUNT(*) FROM nexus_dev.gold.expense_audit;
-- > 0 → seguir
```

## 3. Vector Search (una sola vez + re-sync cuando el gold crezca)

Subir el notebook `nexus-medallion/src/vector/setup_vector_search.py` a
Databricks y ejecutarlo. Crea:

1. `nexus_dev.gold.expense_chunks` (Delta, CDF on, particionada por `tenant_id`).
2. Endpoint `nexus-vs-dev` (STANDARD).
3. Delta Sync Index `nexus_dev.vector.expense_chunks_index` con embedding
   `databricks-bge-large-en` y `pipeline_type=TRIGGERED`.
4. Smoke test — imprime 3 resultados para un tenant real.

Cuando entren lotes grandes de approvals, re-trigger el sync desde el
notebook (celda "Triggering sync…") o llamar `idx.sync()` manualmente.

## 4. Levantar el worker

```bash
cd nexus-orchestration
uv sync
TASK_QUEUE=nexus-rag-tq uv run python -m nexus_orchestration.main_worker
```

Si falta cualquier credencial, el proceso aborta con:

```
Missing credentials for real providers:
  - Vector Search real mode requires: DATABRICKS_HOST, ...
  - SQL search real mode requires: ...
```

## 5. Smoke test end-to-end

1. Asegúrate de tener al menos un expense aprobado en `gold.expense_audit`
   para tu tenant.
2. Backend arriba: `cd backend && uv sync && uv run uvicorn nexus_backend.main:app`.
3. Frontend arriba: `cd frontend && pnpm dev`.
4. Abrir `/chat`, preguntar:
   - "¿cuánto gasté en Uber en marzo?" → debe llamar `search_expenses_structured`,
     responder con el total + citas `[ver recibo](/expenses/...)`.
   - "recibos sobre viajes" → debe llamar `search_expenses_semantic`.
5. Confirmar en logs estructurados la entrada `rag.tool_call` con `tool`,
   `outcome`, `latency_ms`, `row_count`.

## 6. Fallos conocidos y cómo diagnosticar

| Síntoma | Diagnóstico |
|---|---|
| `RESOURCE_DOES_NOT_EXIST` en vector_similarity_search | El index no existe o no está READY. Ejecutar el notebook de setup y esperar a que el smoke test imprima resultados. |
| `UNAUTHENTICATED` en SQL | Token expirado o warehouse apagado. Rotar `DATABRICKS_TOKEN`, encender el warehouse. |
| Chat nunca emite `chat.complete` | Revisar `chat.token` en Redis (`redis-cli monitor | grep chat.token`). Si tampoco aparece, Bedrock está fallando — ver logs del worker. |
| Citations vacías con datos reales | Posible `delete` inyectado por el LLM (no debería pasar); revisa el log `rag.tool_call` — si `row_count=0` la causa es upstream (gold vacío o index desfasado). |
| Preflight: "BEDROCK_MODEL_ID is required" | `FAKE_BEDROCK=false` sin `BEDROCK_MODEL_ID`. Setearlo en `.env`. |

## 7. Volver a fake para una iteración rápida

```
FAKE_PROVIDERS=true
```

El worker arranca sin preflight y usa stubs determinísticos.
