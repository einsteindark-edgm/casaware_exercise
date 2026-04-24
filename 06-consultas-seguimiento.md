# Consultas de seguimiento — MongoDB y Databricks (medallion)

Cheat-sheet para seguir el ciclo de vida de un `expense_id` de punta a punta:

```
POST /expenses  →  Mongo (expenses/receipts/expense_events)
               →  Worker Temporal  →  Mongo (ocr_extractions, hitl_tasks, expense_events, expenses.status)
               →  CDC Listener  →  S3 JSONL.gz
               →  Autoloader + DLT  →  Databricks bronze.mongodb_cdc_* → silver.* → gold.*
```

Cambia `exp_XXXXXXXXXXXXXX` por el `expense_id` real, y `t_alpha` por tu tenant.

---

## 1. MongoDB (nexus_dev) — fuente de verdad transaccional

El backend conecta con `mongodb_db="nexus_dev"`. Colecciones:
`expenses`, `receipts`, `expense_events`, `hitl_tasks`, `ocr_extractions`,
`chat_sessions`, `chat_turns`.

Ejecuta con:

```bash
# Local (si arrancaste el compose dev)
mongosh "mongodb://localhost:27017/nexus_dev"

# Contra ECS (DocumentDB/Mongo en la nube) — usa el URI del Secrets Manager
MONGO_URI=$(aws secretsmanager get-secret-value \
  --secret-id nexus-dev-edgm/mongodb_uri \
  --query SecretString --output text)
mongosh "$MONGO_URI"
```

### 1.1 Estado transaccional del expense

```javascript
// Una expense específica
db.expenses.findOne({ expense_id: "exp_XXXXXXXXXXXXXX" })

// Últimas 10 expenses de un tenant (usa el índice tenant_id+status+created_at)
db.expenses.find(
  { tenant_id: "t_alpha" },
  { _id: 0, expense_id: 1, status: 1, amount: 1, vendor: 1, date: 1, workflow_id: 1, created_at: 1 }
).sort({ created_at: -1 }).limit(10)

// Conteo de expenses por status (dashboard)
db.expenses.aggregate([
  { $match: { tenant_id: "t_alpha" } },
  { $group: { _id: "$status", n: { $sum: 1 }, total: { $sum: "$amount" } } },
  { $sort: { n: -1 } }
])

// Expenses atascadas en processing más de 10 minutos (alerta workflow colgado)
db.expenses.find({
  status: "processing",
  updated_at: { $lt: new Date(Date.now() - 10*60*1000) }
}, { _id: 0, expense_id: 1, workflow_id: 1, updated_at: 1 })
```

### 1.2 Timeline de eventos (`expense_events`)

```javascript
// Timeline completo de una expense en orden cronológico
db.expense_events.find(
  { expense_id: "exp_XXXXXXXXXXXXXX" },
  { _id: 0, event_id: 1, event_type: 1, actor: 1, created_at: 1, details: 1 }
).sort({ created_at: 1 })

// Qué event_types emite el worker vs el backend (sanity check)
db.expense_events.aggregate([
  { $group: { _id: { event_type: "$event_type", actor_type: "$actor.type" }, n: { $sum: 1 } } },
  { $sort: { n: -1 } }
])

// Expenses que nunca llegaron a OCR (no hay ocr_started después de created)
db.expense_events.aggregate([
  { $group: { _id: "$expense_id", types: { $addToSet: "$event_type" } } },
  { $match: { "types": "created", "types": { $ne: "ocr_started" } } },
  { $limit: 20 }
])
```

### 1.3 Recibos subidos a S3

```javascript
// Recibo por expense_id (el s3_key es lo que lee Textract)
db.receipts.findOne(
  { expense_id: "exp_XXXXXXXXXXXXXX" },
  { _id: 0, receipt_id: 1, s3_bucket: 1, s3_key: 1, mime_type: 1, size_bytes: 1, uploaded_at: 1 }
)

// Recibos por usuario
db.receipts.find(
  { tenant_id: "t_alpha", user_id: "sub-123" },
  { _id: 0, receipt_id: 1, expense_id: 1, uploaded_at: 1 }
).sort({ uploaded_at: -1 }).limit(20)
```

### 1.4 OCR (`ocr_extractions`) — escrito por el worker

```javascript
// OCR de una expense
db.ocr_extractions.findOne({ expense_id: "exp_XXXXXXXXXXXXXX" })

// OCR con baja confianza (<80) que debería ir a HITL
db.ocr_extractions.find(
  { avg_confidence: { $lt: 80 } },
  { _id: 0, expense_id: 1, avg_confidence: 1,
    ocr_total: 1, ocr_vendor: 1, ocr_date: 1, textract_raw_s3_key: 1, extracted_at: 1 }
).sort({ extracted_at: -1 }).limit(20)

// Todas las expenses donde OCR y user_reported no coinciden en monto
// (diagnóstico de discrepancias antes de HITL)
db.ocr_extractions.aggregate([
  { $lookup: {
      from: "expenses", localField: "expense_id",
      foreignField: "expense_id", as: "exp"
  }},
  { $unwind: "$exp" },
  { $project: {
      _id: 0, expense_id: 1,
      ocr_total: "$ocr_total",
      user_amount: "$exp.amount",
      diff_pct: {
        $abs: { $divide: [
          { $subtract: [ "$ocr_total", "$exp.amount" ] },
          { $cond: [ { $gt: ["$exp.amount", 0] }, "$exp.amount", 1 ] }
        ]}
      },
      avg_confidence: 1,
      extracted_at: 1
  }},
  { $match: { diff_pct: { $gt: 0.01 } } },
  { $sort: { extracted_at: -1 } },
  { $limit: 20 }
])
```

### 1.5 Tareas HITL

```javascript
// Tareas pendientes en toda la plataforma
db.hitl_tasks.find(
  { status: "pending" },
  { _id: 0, task_id: 1, expense_id: 1, tenant_id: 1,
    fields_in_conflict: 1, created_at: 1 }
).sort({ created_at: 1 })

// Tiempo promedio de resolución HITL por tenant (últimos 7 días)
db.hitl_tasks.aggregate([
  { $match: { status: "resolved",
              created_at: { $gte: new Date(Date.now() - 7*24*3600*1000) } } },
  { $project: {
      tenant_id: 1, decision: 1,
      duration_sec: { $divide: [
        { $subtract: ["$resolved_at", "$created_at"] }, 1000
      ]}
  }},
  { $group: {
      _id: "$tenant_id",
      n: { $sum: 1 },
      avg_sec: { $avg: "$duration_sec" },
      p95_sec: { $percentile: { input: "$duration_sec", p: [0.95], method: "approximate" } }
  }}
])

// HITL de una expense específica (ver qué campos están en conflicto)
db.hitl_tasks.find(
  { expense_id: "exp_XXXXXXXXXXXXXX" },
  { _id: 0, task_id: 1, status: 1, fields_in_conflict: 1,
    decision: 1, resolved_fields: 1, resolved_at: 1, resolved_by: 1 }
)
```

### 1.6 Chat RAG (`chat_sessions` + `chat_turns`)

```javascript
// Sesiones recientes de un usuario
db.chat_sessions.find(
  { tenant_id: "t_alpha", user_id: "sub-123" },
  { _id: 0, session_id: 1, created_at: 1, last_turn: 1 }
).sort({ created_at: -1 }).limit(10)

// Turnos de una sesión (orden cronológico)
db.chat_turns.find(
  { session_id: "sess_XXXXXXXXXXXXXX" },
  { _id: 0, turn: 1, user_message: 1, assistant_message: 1, citations: 1, created_at: 1 }
).sort({ turn: 1 })

// Top N preguntas del día por tenant
db.chat_turns.aggregate([
  { $match: { tenant_id: "t_alpha",
              created_at: { $gte: new Date(Date.now() - 24*3600*1000) } } },
  { $group: { _id: "$user_message", n: { $sum: 1 } } },
  { $sort: { n: -1 } },
  { $limit: 10 }
])
```

### 1.7 Diagnóstico end-to-end (un solo expense, todas las colecciones)

```javascript
const eid = "exp_XXXXXXXXXXXXXX";
print("== expense ==");           printjson(db.expenses.findOne({ expense_id: eid }));
print("== receipt ==");           printjson(db.receipts.findOne({ expense_id: eid }));
print("== ocr ==");               printjson(db.ocr_extractions.findOne({ expense_id: eid }));
print("== hitl ==");               db.hitl_tasks.find({ expense_id: eid }).forEach(printjson);
print("== events (timeline) =="); db.expense_events.find(
  { expense_id: eid },
  { _id: 0, event_type: 1, actor: 1, created_at: 1 }
).sort({ created_at: 1 }).forEach(printjson);
```

### 1.8 Verificar que el listener CDC leyó el cambio

El listener `nexus-cdc` usa `resume tokens`. Aunque el resume token vive en el
oplog/change-stream, puedes confirmar que hubo actividad reciente con:

```javascript
// Último updated_at por colección — si éste es más reciente que el
// __source_ts_ms en bronze, el CDC está atrasado.
db.expenses.find({}, { _id: 0, expense_id: 1, updated_at: 1 })
  .sort({ updated_at: -1 }).limit(1)
db.ocr_extractions.find({}, { _id: 0, expense_id: 1, extracted_at: 1 })
  .sort({ extracted_at: -1 }).limit(1)
db.expense_events.find({}, { _id: 0, event_type: 1, created_at: 1 })
  .sort({ created_at: -1 }).limit(1)
```

---

## 2. Databricks (`nexus_dev` catalog) — medallion

Ejecuta en el SQL Editor o vía `databricks sql` CLI contra
`https://dbc-cd7c46c8-d871.cloud.databricks.com`.

### 2.1 Bronze — CDC crudo (Autoloader desde S3)

Campos **aplanados** al top level + metadata `__op`, `__source_ts_ms`,
`__deleted`, `_cdc_ingestion_ts`.

```sql
-- Conteo por tabla y último ingest (salud del pipeline CDC)
SELECT 'expenses'        AS src, COUNT(*) AS rows, MAX(_cdc_ingestion_ts) AS last_ingest FROM nexus_dev.bronze.mongodb_cdc_expenses
UNION ALL SELECT 'receipts',        COUNT(*), MAX(_cdc_ingestion_ts) FROM nexus_dev.bronze.mongodb_cdc_receipts
UNION ALL SELECT 'ocr_extractions', COUNT(*), MAX(_cdc_ingestion_ts) FROM nexus_dev.bronze.mongodb_cdc_ocr_extractions
UNION ALL SELECT 'hitl_tasks',      COUNT(*), MAX(_cdc_ingestion_ts) FROM nexus_dev.bronze.mongodb_cdc_hitl_tasks
UNION ALL SELECT 'expense_events',  COUNT(*), MAX(_cdc_ingestion_ts) FROM nexus_dev.bronze.mongodb_cdc_expense_events
ORDER BY last_ingest DESC;

-- Últimos expenses que entraron (todas las ops)
SELECT __op, __source_ts_ms, expense_id, tenant_id, status, amount, vendor, date, created_at, updated_at
FROM   nexus_dev.bronze.mongodb_cdc_expenses
ORDER BY __source_ts_ms DESC
LIMIT 20;

-- OCR raw por expense (debug E2E)
SELECT __source_ts_ms, __op, expense_id, avg_confidence,
       ocr_total, ocr_total_confidence,
       ocr_vendor, ocr_vendor_confidence,
       ocr_date, ocr_date_confidence,
       ocr_currency, textract_raw_s3_key, extracted_at
FROM   nexus_dev.bronze.mongodb_cdc_ocr_extractions
WHERE  expense_id = 'exp_XXXXXXXXXXXXXX'
ORDER BY __source_ts_ms;

-- Receipt del expense
SELECT receipt_id, expense_id, tenant_id, s3_key, content_type, size_bytes, uploaded_at, __op
FROM   nexus_dev.bronze.mongodb_cdc_receipts
WHERE  expense_id = 'exp_XXXXXXXXXXXXXX'
ORDER BY __source_ts_ms DESC;

-- Timeline de eventos en bronze (ULID único por evento)
SELECT __source_ts_ms, __op, event_id, expense_id, event_type, actor, workflow_id, details, created_at
FROM   nexus_dev.bronze.mongodb_cdc_expense_events
WHERE  expense_id = 'exp_XXXXXXXXXXXXXX'
ORDER BY __source_ts_ms;
```

### 2.2 Silver — SCD1 aplicado (DLT `apply_changes`)

Sin columnas `__op/__source_ts_ms`; timestamps ya casteados.

```sql
-- Estado actual del expense
SELECT expense_id, tenant_id, user_id, receipt_id, workflow_id,
       status, amount, currency, vendor, date, category,
       final_amount, final_currency, final_vendor, final_date,
       source_per_field, approved_at, created_at, updated_at
FROM   nexus_dev.silver.expenses
WHERE  expense_id = 'exp_XXXXXXXXXXXXXX';

-- Timeline completo (key = event_id)
SELECT event_id, expense_id, event_type, actor, workflow_id, details, created_at
FROM   nexus_dev.silver.expense_events
WHERE  expense_id = 'exp_XXXXXXXXXXXXXX'
ORDER BY created_at;

-- OCR normalizado (lo que lee el Auditor cuando AUDIT_SOURCE=databricks)
SELECT expense_id, tenant_id, user_id,
       ocr_total, ocr_total_confidence,
       ocr_vendor, ocr_vendor_confidence,
       ocr_date, ocr_date_confidence,
       ocr_currency, avg_confidence, textract_raw_s3_key, extracted_at
FROM   nexus_dev.silver.ocr_extractions
WHERE  expense_id = 'exp_XXXXXXXXXXXXXX';

-- Tareas HITL por tenant (conteo)
SELECT status, decision, COUNT(*) AS n
FROM   nexus_dev.silver.hitl_events
WHERE  tenant_id = 't_alpha'
GROUP BY status, decision;

-- Detalle de una HITL
SELECT task_id, expense_id, workflow_id, status, decision,
       discrepancy_fields, resolved_fields,
       resolved_by, resolved_at, created_at
FROM   nexus_dev.silver.hitl_events
WHERE  expense_id = 'exp_XXXXXXXXXXXXXX';
```

### 2.3 Gold — solo aprobados + chunks RAG

```sql
-- Expense aprobado (solo aparece tras status='approved')
SELECT tenant_id, expense_id, user_id, receipt_id,
       final_amount, final_currency, final_vendor, final_date,
       category, source_per_field, approved_at
FROM   nexus_dev.gold.expense_audit
WHERE  expense_id = 'exp_XXXXXXXXXXXXXX';

-- KPI del día por tenant (solo aprobados)
SELECT tenant_id,
       COUNT(*)          AS approved_count,
       SUM(final_amount) AS total_approved_amount
FROM   nexus_dev.gold.expense_audit
WHERE  approved_at >= current_date() - INTERVAL 1 DAY
GROUP BY tenant_id;

-- Dashboard completo de estados — usa silver (gold solo tiene aprobados)
SELECT tenant_id, status, COUNT(*) AS n, SUM(amount) AS sum_amount
FROM   nexus_dev.silver.expenses
WHERE  created_at >= current_date() - INTERVAL 1 DAY
GROUP BY tenant_id, status
ORDER BY tenant_id, status;

-- Chunks indexados en Vector Search
SELECT chunk_id, tenant_id, expense_id, chunk_text,
       amount, vendor, date, category, approved_at
FROM   nexus_dev.gold.expense_chunks
WHERE  tenant_id = 't_alpha'
ORDER BY approved_at DESC
LIMIT 10;
```

### 2.4 Frescura bronze → silver → gold

```sql
WITH b AS (
  SELECT expense_id, MAX(__source_ts_ms) AS bronze_ts_ms
  FROM   nexus_dev.bronze.mongodb_cdc_expenses
  GROUP BY expense_id
),
s AS (
  SELECT expense_id, updated_at AS silver_ts, status
  FROM   nexus_dev.silver.expenses
),
g AS (
  SELECT expense_id, approved_at AS gold_ts
  FROM   nexus_dev.gold.expense_audit
)
SELECT b.expense_id,
       TIMESTAMP_MILLIS(b.bronze_ts_ms)                                             AS bronze_latest,
       s.status,
       s.silver_ts,
       g.gold_ts,
       (unix_millis(s.silver_ts) - b.bronze_ts_ms)/1000.0                           AS bronze_to_silver_sec,
       (unix_millis(g.gold_ts)   - unix_millis(s.silver_ts))/1000.0                 AS silver_to_gold_sec
FROM b
LEFT JOIN s USING(expense_id)
LEFT JOIN g USING(expense_id)
ORDER BY bronze_latest DESC
LIMIT 20;
```

---

## 3. Alineación entre capas (checks rápidos)

```sql
-- ¿Hay expenses en Mongo que NO llegaron a silver?
-- (corre esto en Mongo primero para obtener la lista, luego la cruzas)
-- Databricks:
SELECT 'in_silver' AS layer, COUNT(DISTINCT expense_id) FROM nexus_dev.silver.expenses
UNION ALL
SELECT 'in_bronze', COUNT(DISTINCT expense_id) FROM nexus_dev.bronze.mongodb_cdc_expenses;
```

```javascript
// Mongo — cuenta de expenses (debe coincidir con bronze si CDC está al día)
db.expenses.countDocuments({});
db.expenses.countDocuments({ tenant_id: "t_alpha" });
```

Si `count(bronze) < count(mongo)` → revisa logs de `nexus-cdc`
(`aws logs tail /ecs/nexus-dev-edgm/cdc`) y que el bucket
`s3://nexus-dev-edgm-cdc/expenses/` tenga archivos recientes.

---

## 4. Correcciones respecto a versiones previas de estas consultas

1. Los campos en bronze **no** están anidados en `fullDocument`; el listener los aplana al top level.
2. La columna de ingest es `_cdc_ingestion_ts` (no `_ingest_ts`); el timestamp del origen es `__source_ts_ms`.
3. `ocr_total`, `ocr_vendor`, `ocr_date` son columnas planas en bronze/silver (no structs con `.value`/`.confidence`). La confianza está en `ocr_total_confidence`, `ocr_vendor_confidence`, `ocr_date_confidence`.
4. `silver.ocr_extractions` usa `textract_raw_s3_key`, no `raw_output_s3_key`.
5. `silver.expenses` ya trae embebidos `final_amount / final_vendor / final_date / final_currency` y `source_per_field` (los escribe el Worker al aprobar).
6. `gold.expense_audit` solo contiene aprobados — para ver `hitl_required` / `rejected` usa `silver.expenses`.
7. `gold.expense_chunks` tiene `chunk_text` (no `content`); la metadata está desnormalizada en columnas: `amount`, `vendor`, `date`, `category`.
8. En Mongo, `actor` es un objeto `{ type, id }` (no un string); `details` es un objeto libre; `resolved_fields` en HITL es un mapa `{ field: value }`.
