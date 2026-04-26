# Origen de los datos del dashboard "Expense Trace"

Este documento explica de dónde sale la información que ves en cada widget
del dashboard `Nexus — Expense Trace (bronze→silver→gold)`. Sirve para
aprender cómo fluyen los datos en el proyecto y para diagnosticar cuando un
widget aparece vacío.

URL: https://dbc-01d95494-7b54.cloud.databricks.com/dashboardsv3/01f13fdd03f4144e943cdf2c2543b212

---

## 1. El flujo completo (vista de pájaro)

```
┌────────────────┐   POST /expenses        ┌───────────┐  oplog  ┌──────────────┐
│  Frontend Next │ ──────────────────────► │  Backend  │ ───────►│ nexus-cdc    │
│                │                          │  FastAPI  │         │  listener    │
└────────────────┘                          └─────┬─────┘         └──────┬───────┘
                                                  │                      │
                                                  │ inserts/updates      │ JSONL.gz
                                                  ▼                      ▼
                              ┌────────────────────────────┐   ┌─────────────────────┐
                              │  MongoDB Atlas (nexus_dev) │   │  S3 (cdc bucket)    │
                              │  expenses, receipts,       │   │  /expenses/...      │
                              │  ocr_extractions,          │   │  /receipts/...      │
                              │  hitl_tasks, expense_events│   │  /ocr_extractions/..│
                              └────────────────────────────┘   │  /hitl_tasks/...    │
                                         ▲                      │  /expense_events/...│
                                         │                      └──────────┬──────────┘
                                         │ inserts/updates                 │
                                         │                                 ▼
                              ┌────────────────────────────┐   ┌─────────────────────┐
                              │  Worker Temporal           │   │ Databricks Autoloader│
                              │  (nexus-orchestration)     │   │  + DLT bronze        │
                              └────────────────────────────┘   └──────────┬──────────┘
                                                                          │
                                              ┌───────────────────────────▼────────────────────────┐
                                              │  nexus_dev.bronze.mongodb_cdc_*  (Delta, CDC crudo) │
                                              └───────────────────────┬─────────────────────────────┘
                                                                      │ DLT apply_changes (SCD1)
                                                                      ▼
                                              ┌────────────────────────────────────────────────────┐
                                              │  nexus_dev.silver.{expenses, expense_events,       │
                                              │       ocr_extractions, hitl_events}                 │
                                              └───────────────────────┬────────────────────────────┘
                                                                      │ DLT @dlt.table (MV, where status='approved')
                                                                      ▼
                                              ┌────────────────────────────────────────────────────┐
                                              │  nexus_dev.gold.{expense_audit, expense_chunks}    │
                                              └────────────────────────────────────────────────────┘
```

Quién escribe qué en Mongo:

| Colección Mongo         | Creación (insert)                       | Actualizaciones                                     |
|-------------------------|-----------------------------------------|------------------------------------------------------|
| `expenses`              | Backend, al recibir `POST /expenses`    | Worker: `status: pending → processing → approved \| rejected` + `final_*` |
| `receipts`              | Backend                                 | —                                                    |
| `expense_events`        | Backend (`created`, `hitl_resolved`)    | Worker: `ocr_started`, `ocr_completed`, `discrepancy_detected`, `hitl_required`, `approved`, `failed` |
| `ocr_extractions`       | Worker (después de Textract)            | Worker (upsert por `expense_id`)                     |
| `hitl_tasks`            | Worker (cuando detecta discrepancia)    | Backend (cuando el usuario resuelve en `/hitl/{id}/resolve`) |

Todos los cambios en Mongo disparan eventos que el listener `nexus-cdc`
escucha por change-stream y escribe como `.jsonl.gz` en
`s3://nexus-dev-edgm-cdc/{collection}/`. Autoloader de Databricks los
ingesta en `bronze.mongodb_cdc_{collection}` (tipo Delta), y de ahí DLT
los promueve a silver y gold.

---

## 2. Widget por widget

### 2.1 Filter "Select an expense_id"

- **Dropdown de IDs**: `d_all_ids`
  ```sql
  SELECT expense_id, MAX(created_at) AS last_seen
  FROM   nexus_dev.silver.expenses
  GROUP BY expense_id
  ORDER BY last_seen DESC
  LIMIT 500
  ```
- **Origen físico:** tabla `nexus_dev.silver.expenses`, que es SCD1 sobre
  `nexus_dev.bronze.mongodb_cdc_expenses`. Cada expense_id aparece una vez
  (la fila sobrevive los MERGE del CDC).
- **Cómo se popula:**
  - Backend crea la fila cuando recibe `POST /expenses`
    (`backend/src/nexus_backend/api/v1/expenses.py:123`, `status="pending"`).
  - CDC listener ve el insert → escribe JSONL en S3 →
    Autoloader ingesta a bronze → DLT promueve a silver.

### 2.2 Widget "Latest expenses"

- **Dataset:** `d_recent`
  ```sql
  SELECT expense_id, tenant_id, status, amount, currency, vendor, created_at
  FROM   nexus_dev.silver.expenses
  ORDER BY created_at DESC
  LIMIT 20
  ```
- Igual que arriba, misma fuente. Útil para copiar un ID al filtro.

### 2.3 Widget "Does this ID exist in each layer?" (bar)

- **Dataset:** `d_presence`
  ```sql
  SELECT 'bronze' AS layer, COUNT(*) FROM nexus_dev.bronze.mongodb_cdc_expenses WHERE expense_id = :p_expense_id
  UNION ALL SELECT 'silver', COUNT(*) FROM nexus_dev.silver.expenses             WHERE expense_id = :p_expense_id
  UNION ALL SELECT 'gold',   COUNT(*) FROM nexus_dev.gold.expense_audit          WHERE expense_id = :p_expense_id
  ```
- **Qué cuenta cada capa:**
  - **bronze**: 1 fila por cada evento CDC de ese expense (inserts + N updates).
    Si es un expense aprobado después de HITL verás 5-8 filas.
  - **silver**: 1 (SCD1 colapsa todos los eventos CDC en la versión final).
  - **gold**: 1 si `status='approved'`, 0 en cualquier otro caso.
- **Cómo interpretar:**
  - bronze=0 → el expense nunca llegó a Databricks (listener caído, o S3 sin subir).
  - bronze>0 silver=0 → DLT bronze→silver está fallando. Revisa pipeline `nexus-silver-...`.
  - silver=1 gold=0 → el expense aún no está aprobado, o pipeline gold falló.

### 2.4 Widget "Silver — current state" (tabla principal)

- **Dataset:** `d_silver_expense`
  ```sql
  SELECT expense_id, tenant_id, user_id, receipt_id, workflow_id,
         status, amount, currency, vendor, date, category,
         final_amount, final_currency, final_vendor, final_date,
         approved_at, created_at, updated_at
  FROM   nexus_dev.silver.expenses
  WHERE  expense_id = :p_expense_id
  ```
- **Origen:** `silver.expenses` (SCD1 keyed por `expense_id`).
- **Por qué los dos conjuntos de columnas:**
  - `amount / vendor / date` son lo que el usuario reportó al crear la expense.
  - `final_amount / final_vendor / final_date` son el resultado definitivo después
    del OCR + (opcional) HITL, escritos por el worker en la transición a `approved`
    (`nexus-orchestration/src/nexus_orchestration/activities/mongodb_writes.py:update_expense_to_approved`).
- **Estados posibles** (evolución cronológica):
  `pending → processing → approved` (feliz)
  `pending → processing → hitl_required → approved` (con HITL)
  `pending → processing → rejected` (HITL rechazado)
  `pending → processing → failed` (OCR o workflow falló)

### 2.5 Widget "OCR extraction (silver)"

- **Dataset:** `d_silver_ocr`
  ```sql
  SELECT ocr_total, ocr_total_confidence,
         ocr_vendor, ocr_vendor_confidence,
         ocr_date, ocr_date_confidence,
         ocr_currency, avg_confidence, textract_raw_s3_key, extracted_at
  FROM   nexus_dev.silver.ocr_extractions
  WHERE  expense_id = :p_expense_id
  ```
- **Origen:** `silver.ocr_extractions` (SCD1 sobre `bronze.mongodb_cdc_ocr_extractions`).
- **Cómo llega la fila:**
  1. El worker `OCRExtractionWorkflow`
     (`nexus-orchestration/src/nexus_orchestration/workflows/ocr_extraction.py`)
     llama a AWS Textract con el S3 key del recibo.
  2. Textract devuelve el JSON crudo; el worker lo normaliza a total/vendor/date +
     sus confidences (`activities/textract.py`).
  3. El worker escribe la fila con `upsert_ocr_extraction`
     (`activities/mongodb_writes.py:34`). Una fila por `expense_id`; si se re-ejecuta
     el workflow, hace upsert.
  4. Mongo → CDC → bronze → silver (igual que los expenses).
- **Interpretación de confidence:**
  - Textract reporta un 0-100 por cada campo.
  - `avg_confidence` es el promedio. Si está `<80`, el workflow fuerza HITL por
    regla (`workflows/ocr_extraction.py`).
  - `textract_raw_s3_key` apunta al JSON completo en `s3://nexus-dev-edgm-textract-output/`,
    por si quieres auditar qué devolvió Textract.

### 2.6 Widget "HITL tasks"

- **Dataset:** `d_silver_hitl`
  ```sql
  SELECT task_id, status, decision, resolved_by, resolved_at, created_at, workflow_id
  FROM   nexus_dev.silver.hitl_events
  WHERE  expense_id = :p_expense_id
  ORDER BY created_at DESC
  ```
- **Origen:** `silver.hitl_events` (SCD1 sobre `bronze.mongodb_cdc_hitl_tasks`,
  key = `task_id`).
- **Cómo llega la fila:**
  1. El worker `AuditValidationWorkflow` corre `compare_fields`
     (`activities/comparison.py`) sobre user_reported vs OCR. Si hay discrepancia
     (>1% en monto, o <85% similitud en vendor, o date distinto), crea una
     hitl_task con `status="pending"` (`activities/mongodb_writes.py:create_hitl_task`).
  2. El frontend muestra la tarea al auditor.
  3. Al resolver, el backend (`POST /hitl/{id}/resolve`) actualiza `status="resolved"`,
     `decision`, `resolved_fields`, `resolved_by`, `resolved_at`
     (`backend/src/nexus_backend/api/v1/hitl.py:76`).
  4. Ambos cambios (insert por worker + update por backend) pasan por CDC.
- **Columnas clave:**
  - `status`: `pending` | `resolved`.
  - `decision`: `approve` (aceptar user-reported), `edit` (auditor corrigió a mano),
    `reject` (rechazar la expense).

### 2.7 Widget "Events timeline" ← LO QUE PREGUNTASTE

- **Dataset:** `d_silver_timeline`
  ```sql
  SELECT created_at, event_type, actor, workflow_id, event_id
  FROM   nexus_dev.silver.expense_events
  WHERE  expense_id = :p_expense_id
  ORDER BY created_at
  ```
- **Origen:** `silver.expense_events` (append-only, SCD1 por `event_id` que es único).
- **Qué es esta tabla:** un **log inmutable** — una fila por cada cambio semántico
  en la vida de la expense. A diferencia de `silver.expenses` (que tiene UNA fila
  por expense_id con el estado final), `expense_events` tiene MUCHAS filas por
  expense_id — cada fila registra un cambio de estado con timestamp y quién lo hizo.
- **Quién emite cada `event_type`:**
  | event_type                | Emisor     | Cuándo |
  |---------------------------|------------|--------|
  | `created`                 | Backend    | Al recibir `POST /expenses` |
  | `ocr_started`             | Worker     | Al arrancar `OCRExtractionWorkflow` |
  | `ocr_completed`           | Worker     | Textract devolvió resultado OK |
  | `ocr_failed`              | Worker     | Textract falló tras retries |
  | `no_discrepancy`          | Worker     | `compare_fields` dio vacío → auto-aprobar |
  | `discrepancy_detected`    | Worker     | `compare_fields` encontró conflictos |
  | `hitl_required`           | Worker     | Creó hitl_task y está esperando |
  | `hitl_resolved`           | Backend    | Auditor resolvió la tarea |
  | `approved`                | Worker     | Transición final a `approved` |
  | `failed`                  | Worker     | Workflow abortó (timeout, cancel, crash) |
- **Quién escribe:**
  - Worker: `activities/mongodb_writes.py:emit_expense_event` inserta en Mongo
    `expense_events` con `actor={type:"system", id:"worker"}`.
  - Backend: hace lo mismo en `expenses.py:141` y `hitl.py:92` con
    `actor={type:"user", id:<sub>}` o `{type:"backend"}`.
- **Cómo usarlo:**
  El timeline te dice si el workflow se atascó. Ejemplos:
  - Solo ves `created` → el workflow nunca arrancó (problema con Temporal o el
    backend no disparó el workflow).
  - `created → ocr_started` pero no `ocr_completed` → Textract está pegado o el
    worker se murió.
  - `...→ hitl_required` sin `hitl_resolved` durante varias horas → tarea HITL
    esperando acción humana.
  - `...→ approved` pero ninguna fila en gold → pipeline gold está fallando.
- **Útil:** `event_id` es un ULID — si filtras por `workflow_id` en otra query,
  ves todos los eventos de ese workflow, incluso si tocó múltiples expenses
  (no aplica hoy pero el schema lo soporta).

### 2.8 Widget "Gold — expense_audit"

- **Dataset:** `d_gold_audit`
  ```sql
  SELECT tenant_id, expense_id, user_id, receipt_id,
         final_amount, final_currency, final_vendor, final_date,
         category, approved_at
  FROM   nexus_dev.gold.expense_audit
  WHERE  expense_id = :p_expense_id
  ```
- **Origen:** `gold.expense_audit`, que es un **materialized view**
  (`nexus-medallion/src/gold/expense_audit.py`) con el siguiente SQL implícito:
  ```python
  dlt.read("silver.expenses").where("status = 'approved'").select(...)
  ```
- **Por qué puede estar vacío aunque silver tenga la fila:**
  1. El expense aún no llegó a `status='approved'` (sigue en HITL, processing, etc.).
  2. La pipeline `nexus-gold-nexus_dev` está fallando (antes fallaba por
     `dlt.read_stream` con MERGE — ese bug ya se arregló).
  3. El pipeline gold no ha corrido desde que silver cambió
     (DLT re-ejecuta el MV en un schedule o full refresh).
- **No tiene `source_per_field` ni timestamps intermedios** — gold es deliberadamente
  delgado, pensado para BI y RAG. Si necesitas trazabilidad fina, usa silver.

### 2.9 Widget "Bronze CDC (all change events)"

- **Dataset:** `d_bronze_cdc_expense`
  ```sql
  SELECT __source_ts_ms, __op, __deleted, tenant_id, status, amount, currency, vendor,
         date, final_amount, final_vendor, final_date, created_at, updated_at,
         _cdc_ingestion_ts
  FROM   nexus_dev.bronze.mongodb_cdc_expenses
  WHERE  expense_id = :p_expense_id
  ORDER BY __source_ts_ms
  ```
- **Origen:** `bronze.mongodb_cdc_expenses` (Delta append-only, no dedupe).
- **Cómo llega cada fila:**
  1. En Mongo Atlas se registra un change-stream event por cada insert/update/delete
     (MongoDB "op": `i`/`u`/`d`).
  2. `nexus-cdc/src/nexus_cdc/listener.py` suscribe al change-stream, serializa a
     JSON y lo sube en lotes `.jsonl.gz` a `s3://nexus-dev-edgm-cdc/expenses/…/*.jsonl.gz`.
  3. El pipeline `nexus-bronze-cdc-nexus_dev` usa Autoloader
     (`nexus-medallion/src/bronze/cdc_expenses.py`) para leer esos archivos de S3
     y apendearlos a la tabla Delta bronze.
- **Columnas de metadata CDC:**
  - `__op`: `i` (insert) | `u` (update) | `d` (delete).
  - `__source_ts_ms`: timestamp epoch en ms del evento en Mongo (el "source of truth" del orden).
  - `__deleted`: flag booleano redundante con `__op='d'`.
  - `_cdc_ingestion_ts`: cuándo Autoloader ingirió el archivo (no cuándo ocurrió el cambio).
- **Útil:** esta tabla es la única que te deja reconstruir la historia completa
  del expense (ver cómo fue cambiando el status, cuándo se escribieron los
  `final_*`, etc.). Silver te da el estado actual; bronze te da la película.

### 2.10 Widget "S3 receipt"

- **Dataset:** `d_bronze_receipt`
  ```sql
  SELECT __source_ts_ms, __op, receipt_id, s3_key, content_type, size_bytes, uploaded_at
  FROM   nexus_dev.bronze.mongodb_cdc_receipts
  WHERE  expense_id = :p_expense_id
  ORDER BY __source_ts_ms
  ```
- **Origen:** `bronze.mongodb_cdc_receipts`, que espeja
  `mongodb://nexus_dev/receipts` vía el mismo CDC.
- **Cómo llega:** el backend, cuando recibe el multipart del `POST /expenses`,
  sube el PDF al bucket `s3://nexus-dev-edgm-receipts/` y luego inserta la fila
  en Mongo `receipts` con `s3_key = tenant=.../expense=.../uuid.pdf`
  (`backend/src/nexus_backend/api/v1/expenses.py:110`).
- **Qué puedes hacer con el `s3_key`:** abrirlo con
  `aws s3 cp s3://nexus-dev-edgm-receipts/<s3_key> /tmp/recibo.pdf` para revisar
  el original. También es el input que el worker le pasa a Textract.

---

## 3. Resumen de la taxonomía de tablas

| Tipo           | Nombre               | Grain (key)              | Escribe                      | Lee                        |
|----------------|----------------------|--------------------------|------------------------------|----------------------------|
| Mongo transacc | `expenses`           | `expense_id` + `tenant_id` | backend (create), worker (updates) | frontend API              |
| Mongo transacc | `receipts`           | `receipt_id`             | backend                      | frontend API, worker       |
| Mongo transacc | `expense_events`     | `event_id` (append-only) | backend + worker             | frontend timeline, BI      |
| Mongo transacc | `ocr_extractions`    | `expense_id`             | worker                       | worker (validation)        |
| Mongo transacc | `hitl_tasks`         | `task_id`                | worker (create), backend (resolve) | frontend HITL queue   |
| Delta bronze   | `mongodb_cdc_*`      | append-only por evento   | Autoloader (desde S3)        | DLT silver                 |
| Delta silver   | `expenses`           | `expense_id` (SCD1)      | DLT apply_changes            | gold, BI, auditor workflow |
| Delta silver   | `ocr_extractions`    | `expense_id` (SCD1)      | DLT apply_changes            | auditor workflow, BI       |
| Delta silver   | `hitl_events`        | `task_id` (SCD1)         | DLT apply_changes            | BI, dashboard              |
| Delta silver   | `expense_events`     | `event_id` (SCD1)        | DLT apply_changes            | dashboard, auditoría       |
| Delta gold     | `expense_audit`      | `expense_id` (MV)        | DLT @dlt.table               | BI, RAG                    |
| Delta gold     | `expense_chunks`     | `chunk_id` (MV)          | DLT @dlt.table               | Vector Search              |

---

## 4. Referencias rápidas

- Backend Mongo writes: `backend/src/nexus_backend/api/v1/expenses.py`, `hitl.py`.
- Worker Mongo writes:  `nexus-orchestration/src/nexus_orchestration/activities/mongodb_writes.py`.
- CDC listener:         `nexus-cdc/src/nexus_cdc/listener.py`.
- DLT bronze:           `nexus-medallion/src/bronze/cdc_*.py`.
- DLT silver:           `nexus-medallion/src/silver/*.py`.
- DLT gold:             `nexus-medallion/src/gold/*.py`.
- Consultas manuales:   `06-consultas-seguimiento.md` (Mongo + Databricks).
