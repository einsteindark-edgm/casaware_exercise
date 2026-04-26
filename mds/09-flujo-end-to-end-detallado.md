# Flujo End-to-End Detallado — Nexus Expense Audit

> **Objetivo**: documentar EXACTAMENTE qué clases, archivos y líneas participan en el ciclo de vida de un documento de gasto, desde el `POST /expenses` hasta la aprobación/rechazo final, incluyendo persistencia (S3 + Mongo), orquestación (Temporal), OCR (Textract), HITL y eventos en tiempo real (Redis SSE).
>
> **Convenciones**: rutas absolutas relativas a `/Users/edgm/Documents/Projects/caseware/casaware_exercise`. Las líneas son aproximadas y reflejan el estado del repo a la fecha de la auditoría.

---

## 0. Diagrama narrativo del flujo

```
                        ┌──────────────────────────────────────┐
                        │  Cliente (Next.js / curl)            │
                        │  POST /expenses (multipart)          │
                        └──────────────────┬───────────────────┘
                                           │
                                           ▼
┌──────────────────────────── BACKEND (FastAPI) ─────────────────────────────┐
│                                                                            │
│  api/v1/expenses.py::create_expense()                                      │
│   ├─ _read_upload(file)              (chunks de 1MB, máx 10MB)             │
│   ├─ magic.from_buffer()             (sniff MIME real)                     │
│   ├─ new_expense_id() / new_receipt_id() (ULIDs)                           │
│   ├─ S3Service.upload_bytes()        ──► s3://receipts/tenant=…/user=…/…   │
│   ├─ mongo.db.receipts.insert_one()  (metadata del archivo)                │
│   ├─ mongo.db.expenses.insert_one()  (status="pending")                    │
│   ├─ mongo.db.expense_events.insert  (type="created")                      │
│   ├─ RealTemporalBackend.start_workflow("ExpenseAuditWorkflow", …)         │
│   └─ sse_broker.publish_many()       ──► Redis pub/sub "workflow.started"  │
│                                                                            │
└────────────────────────────────────┬───────────────────────────────────────┘
                                     │ start_workflow (gRPC)
                                     ▼
┌────────────────────── TEMPORAL · nexus-orchestrator-tq ────────────────────┐
│                                                                            │
│  ExpenseAuditWorkflow.run(inp)                                             │
│   ├─ act:update_expense_status   "processing"                              │
│   │                                                                        │
│   ├─ ChildWorkflow OCRExtractionWorkflow  (queue: nexus-ocr-tq)            │
│   │    ├─ act:emit_expense_event   "ocr_started"                           │
│   │    ├─ act:publish_event        "workflow.ocr_progress" 10%             │
│   │    ├─ act:textract_analyze_expense   ──► AWS Textract                  │
│   │    ├─ (fallback) act:textract_analyze_document_queries (si conf < 80%) │
│   │    ├─ act:upsert_ocr_extraction      ──► mongo.ocr_extractions         │
│   │    ├─ act:emit_expense_event   "ocr_completed"                         │
│   │    └─ act:publish_event        "workflow.ocr_progress" 100%            │
│   │                                                                        │
│   ├─ ChildWorkflow AuditValidationWorkflow  (queue: nexus-databricks-tq)   │
│   │    ├─ act:query_expense_for_validation  (Mongo o Databricks SQL)       │
│   │    ├─ act:compare_fields                (Levenshtein + 1% tolerancia)  │
│   │    ├─ act:create_hitl_task              ──► mongo.hitl_tasks  (si ≠)   │
│   │    └─ act:emit_expense_event  "discrepancy_detected"|"no_discrepancy"  │
│   │                                                                        │
│   ├─ if has_discrepancies:                                                 │
│   │    ├─ act:update_expense_status "hitl_required"                        │
│   │    ├─ act:emit_expense_event    "hitl_required"                        │
│   │    ├─ act:publish_event         "workflow.hitl_required"               │
│   │    └─ workflow.wait_condition(_hitl_response, timeout=7 días)          │
│   │                                                                        │
│   ├─ act:update_expense_to_approved (o _to_rejected si timeout)            │
│   ├─ act:emit_expense_event   "approved"|"rejected"                        │
│   └─ act:publish_event        "workflow.completed"|"workflow.failed"       │
│                                                                            │
└────────────────────────────────────┬───────────────────────────────────────┘
                                     │
        (Mongo Change Streams) ──────┼───► Debezium → Kafka → Bronze/Silver/Gold
                                     │
                                     ▼
       ┌──── Frontend SSE: GET /workflows/{wf}/stream ◄── Redis pub/sub ────┐
       │  Si HITL:  POST /hitl/{task_id}/resolve  ──► signal hitl_response │
       └────────────────────────────────────────────────────────────────────┘
```

---

## 1. Entrada al Backend (FastAPI)

### 1.1 Endpoint `POST /v1/expenses`

| Item | Valor |
|---|---|
| Archivo | `backend/src/nexus_backend/api/v1/expenses.py` |
| Función | `create_expense()` |
| Líneas | 71 – 195 |
| Decorador | `@router.post("", response_model=ExpenseCreated, status_code=202)` |
| Auth | Dependencia `get_current_user` → resuelve `tenant_id` y `sub` desde JWT |

Firma efectiva:

```python
async def create_expense(
    expense: ExpenseCreate,            # Pydantic: amount, currency, date, vendor, category
    file: UploadFile,                  # archivo binario (jpg/png/pdf)
    user: AuthenticatedUser = Depends(get_current_user),
    s3_service: S3Service = Depends(get_s3_service),
    mongo: MongoConnection = Depends(get_mongo),
    temporal_service: TemporalBackend = Depends(get_temporal_backend),
    sse_broker: SSEEventBroker = Depends(get_sse_broker),
) -> ExpenseCreated
```

### 1.2 Pasos internos (en orden)

1. **Lectura del archivo** — `_read_upload(file)` (mismo módulo). Lee en chunks de 1 MB, corta a 10 MB con `ValidationFailed`.
2. **Detección de MIME real** — `magic.from_buffer(data, mime=True)` (línea ~86). Fallback al `content_type` declarado. Solo `image/jpeg | image/png | application/pdf`.
3. **IDs y workflow_id** (líneas 95–98):
   ```python
   expense_id = new_expense_id()                     # ULID
   receipt_id = new_receipt_id()                     # ULID
   workflow_id = f"expense-audit-{expense_id}"       # determinístico
   ```
4. **Construcción de la S3 key** (línea 99):
   ```
   tenant={tenant_id}/user={sub}/{receipt_id}.{ext}
   ```

---

## 2. Persistencia en S3

| Item | Valor |
|---|---|
| Clase | `S3Service` |
| Archivo | `backend/src/nexus_backend/services/s3.py` |
| Líneas | 10 – 68 |
| Método clave | `upload_bytes(bucket, key, data, content_type)` (líneas 29–38) |
| Cliente | `aioboto3.Session()` → `s3.put_object(...)` |
| Bucket | `settings.s3_receipts_bucket` |

Llamada desde el endpoint (líneas 103–108):

```python
await s3_service.upload_bytes(
    bucket=settings.s3_receipts_bucket,
    key=s3_key,
    data=data,
    content_type=effective_mime,
)
```

---

## 3. Persistencia en MongoDB (escritura inicial)

Tres colecciones se escriben en el endpoint, en este orden:

### 3.1 `receipts` — metadata del binario en S3
Líneas 110–122. Documento:
```json
{
  "receipt_id": "01HX…",
  "tenant_id": "...", "user_id": "...", "expense_id": "01HX…",
  "s3_bucket": "...", "s3_key": "tenant=…/user=…/01HX….pdf",
  "mime_type": "application/pdf", "size_bytes": 12345,
  "uploaded_at": "<utc now>"
}
```

### 3.2 `expenses` — registro principal del gasto
Líneas 124–140. Documento (`status: "pending"`):
```json
{
  "expense_id": "...", "tenant_id": "...", "user_id": "...",
  "amount": 123.45, "currency": "USD",
  "date": "<datetime UTC>", "vendor": "...", "category": "...",
  "receipt_id": "...", "workflow_id": "expense-audit-…",
  "status": "pending",
  "created_at": "...", "updated_at": "..."
}
```

### 3.3 `expense_events` — timeline (evento `created`)
Líneas 142–153. Documento:
```json
{
  "event_id": "01HX…",
  "expense_id": "...", "tenant_id": "...",
  "event_type": "created",
  "actor": {"type": "user", "id": "<sub>"},
  "details": {"user_reported": { ... datos del Pydantic ... }},
  "workflow_id": "expense-audit-…",
  "created_at": "<utc now>"
}
```

---

## 4. Disparo de Temporal

### 4.1 Cliente Temporal en el backend

| Item | Valor |
|---|---|
| Clase | `RealTemporalBackend` |
| Archivo | `backend/src/nexus_backend/services/temporal_client.py` |
| Líneas | 43 – 93 |
| Conexión | `Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)` |
| Método clave | `start_workflow()` (líneas 68–93) — usa `WorkflowIDReusePolicy.REJECT_DUPLICATE` |
| Método auxiliar | `signal()` (líneas 95–106) — usado por HITL |

### 4.2 Llamada desde `create_expense` (líneas 160–174)

```python
await temporal_service.start_workflow(
    "ExpenseAuditWorkflow",
    args=[{
        "expense_id": expense_id,
        "tenant_id": user.tenant_id,
        "user_id": user.sub,
        "receipt_s3_key": s3_key,
        "user_reported_data": expense.model_dump(mode="json"),
    }],
    workflow_id=workflow_id,                   # "expense-audit-{expense_id}"
    task_queue="nexus-orchestrator-tq",
    memo=memo,                                 # X-Ray trace headers si existen
)
```

### 4.3 Notificación SSE inmediata
Líneas 175–193. Se publica al `SSEEventBroker` (Redis) un evento `workflow.started`, en dos canales:
- `nexus:events:tenant:{tenant_id}:user:{user_id}` (canal del usuario)
- `nexus:events:tenant:{tenant_id}:workflow:{workflow_id}` (canal del workflow)

Adicionalmente se incrementa la métrica Prometheus `workflow_starts{workflow_type, tenant_id}`.

El endpoint responde **HTTP 202** con `ExpenseCreated{ expense_id, workflow_id, receipt_id, status: "pending" }`.

---

## 5. Workflow principal: `ExpenseAuditWorkflow`

| Item | Valor |
|---|---|
| Archivo | `nexus-orchestration/src/nexus_orchestration/workflows/expense_audit.py` |
| Clase | `ExpenseAuditWorkflow` |
| Líneas | 25 – 282 |
| Decorador | `@workflow.defn(name="ExpenseAuditWorkflow")` |
| Task queue | `nexus-orchestrator-tq` |
| Worker registrante | `nexus-orchestration/src/nexus_orchestration/main_worker.py` (línea ~107) |
| Estado interno | `self._hitl_response: dict | None`, `self._cancelled: bool`, `self._current_step: str` |

### 5.1 Etapa 0 — `processing`
Líneas 42–47. Activity `update_expense_status` con `{"status":"processing"}`, `start_to_close_timeout=10s`, retry máx. 3.

### 5.2 Etapa 1 — Child `OCRExtractionWorkflow`
Líneas 53–69:
```python
ocr_result = await workflow.execute_child_workflow(
    OCRExtractionWorkflow.run,
    args=[{
        "expense_id": expense_id,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "receipt_s3_key": inp["receipt_s3_key"],
        "s3_bucket": inp.get("s3_bucket"),
        "user_reported_data": inp.get("user_reported_data") or {},
    }],
    id=f"ocr-{expense_id}",
    task_queue="nexus-ocr-tq",
    retry_policy=RetryPolicy(maximum_attempts=3,
                             non_retryable_error_types=["UnsupportedMimeTypeError"]),
)
```

### 5.3 Etapa 2 — Child `AuditValidationWorkflow`
Líneas ~75–88. Se ejecuta tras OCR; recibe los `fields` del OCR + el `expense_id`.

### 5.4 Branch HITL vs aprobación directa
- `if audit_result["has_discrepancies"]:` → flujo HITL (sección 8).
- `else:` → flujo de aprobación directa (sección 9).

### 5.5 Signals y queries expuestos
| Tipo | Nombre | Líneas | Propósito |
|---|---|---|---|
| Signal | `hitl_response` | 256–258 | El backend lo manda cuando un humano resuelve la tarea. |
| Signal | `cancel_audit` | 260–262 | Cancelación administrativa; setea `_cancelled = True`. |
| Query | `get_status` | 265–267 | Devuelve `{state, current_step}` para `GET /workflows/{wf}/status`. |

---

## 6. OCR — `OCRExtractionWorkflow` + Activity Textract

### 6.1 Workflow OCR
| Item | Valor |
|---|---|
| Archivo | `nexus-orchestration/src/nexus_orchestration/workflows/ocr_extraction.py` |
| Clase | `OCRExtractionWorkflow` |
| Líneas | 15 – 139 |
| Task queue | `nexus-ocr-tq` |

Pasos (en orden):

| # | Activity | Líneas | Efecto |
|---|---|---|---|
| 1 | `emit_expense_event` (`ocr_started`) | 24–34 | Inserta en `expense_events`. |
| 2 | `publish_event` (`workflow.ocr_progress`, 10%) | 37–47 | Redis pub/sub. |
| 3 | `textract_analyze_expense` | 50–64 | Llama AWS Textract (sección 6.2). |
| 4 | (opcional) `textract_analyze_document_queries` | 67–95 | Sólo si `avg_confidence < 80`. Luego `_merge_extractions(...)` (líneas 142–167). |
| 5 | `upsert_ocr_extraction` | 98–109 | Escribe en `mongo.ocr_extractions` (sección 7). |
| 6 | `emit_expense_event` (`ocr_completed`) | 112–125 | Timeline. |
| 7 | `publish_event` (`workflow.ocr_progress`, 100%) | 127–137 | Redis pub/sub. |

### 6.2 Activity `textract_analyze_expense`

| Item | Valor |
|---|---|
| Archivo | `nexus-orchestration/src/nexus_orchestration/activities/textract.py` |
| Función | `textract_analyze_expense` |
| Líneas | 33 – 70 |
| Cliente | `boto3.client("textract", region_name=settings.aws_region)` |
| API call | `client.analyze_expense(Document={"S3Object": {"Bucket": bucket, "Name": s3_key}})` |
| Output S3 | `s3://{settings.s3_textract_output_bucket}/tenant={tenant_id}/expense={expense_id}/{uuid}.json` |
| Helper de normalización | `_normalize_summary_fields(...)` (mismo módulo) extrae `TOTAL`, `VENDOR_NAME`, `INVOICE_RECEIPT_DATE` |

Retorno (forma):
```python
{
  "raw_output_s3_key": "...",
  "fields": {
    "ocr_total":  {"value": 123.45, "confidence": 92.1},
    "ocr_vendor": {"value": "ACME",  "confidence": 88.4},
    "ocr_date":   {"value": "2026-04-22", "confidence": 95.0},
  },
  "avg_confidence": 91.8,
  "fields_summary": [ ... ],
}
```

> **Fallback** (líneas 73–96): activity `textract_analyze_document_queries` con tres queries (`TOTAL`, `DATE`, `VENDOR`) si la confianza promedio quedó baja. El merge final compara campo a campo y se queda con la mejor confianza.

---

## 7. Reescritura del OCR a MongoDB

| Item | Valor |
|---|---|
| Archivo | `nexus-orchestration/src/nexus_orchestration/activities/mongodb_writes.py` |
| Activity | `upsert_ocr_extraction` |
| Líneas | 33 – 52 |
| Colección | `mongo.db.ocr_extractions` |
| Operación | `update_one({expense_id}, {"$set": doc}, upsert=True)` |

Documento upserteado:
```python
{
  "tenant_id", "user_id",
  "ocr_total", "ocr_total_confidence",
  "ocr_vendor", "ocr_vendor_confidence",
  "ocr_date",  "ocr_date_confidence",
  "avg_confidence",
  "textract_raw_s3_key",
  "extracted_by_workflow_id": activity.info().workflow_id,
  "extracted_at": <utc now>,
}
```

---

## 8. Validación + creación de tarea HITL

### 8.1 Workflow `AuditValidationWorkflow`
| Item | Valor |
|---|---|
| Archivo | `nexus-orchestration/src/nexus_orchestration/workflows/audit_validation.py` |
| Clase | `AuditValidationWorkflow` |
| Líneas | 15 – 99 |
| Task queue | `nexus-databricks-tq` |

Flujo:

1. **`query_expense_for_validation`** (líneas 26–31) — activity en `activities/query_expense.py:82-94`. En dev lee de `mongo.expenses`; en prod puede usar Databricks SQL (controlado por settings). Devuelve `{amount, currency, date, vendor, category}` del usuario.
2. **`compare_fields`** (líneas 38–47) — activity en `activities/comparison.py:44-98`. Reglas:
   - `amount`: conflicto si `|user-ocr| / user > 1%`.
   - `vendor`: conflicto si `rapidfuzz.ratio(user, ocr) < 85%`.
   - `date`: conflicto si igualdad estricta falla.
3. **`create_hitl_task`** (líneas 53–77, sólo si hay conflictos) — activity en `mongodb_writes.py:118-133`. Inserta:
   ```python
   await _db().hitl_tasks.insert_one({
     "task_id": new_hitl_id(),
     "workflow_id", "tenant_id", "user_id", "expense_id",
     "discrepancy_fields": fields_in_conflict,
     "status": "pending",
     "created_at": <utc now>,
   })
   ```
4. **`emit_expense_event`** (`discrepancy_detected` o `no_discrepancy`).

Retorno (línea 92–98):
```python
{
  "has_discrepancies": bool(fields_in_conflict),
  "fields_in_conflict": [...],
  "hitl_task_id": "01HX…" | None,
  "extracted_data": ocr_result["fields"],
  "user_reported": {...},
}
```

### 8.2 Lado del workflow padre: pausa esperando HITL

En `ExpenseAuditWorkflow.run` (líneas 88–146):

1. `update_expense_status("hitl_required")` (95–104).
2. `emit_expense_event("hitl_required", details={hitl_task_id, fields_in_conflict})` (107–120).
3. `publish_event("workflow.hitl_required")` (121–134) — empuja a Redis para SSE.
4. **Espera bloqueante**:
   ```python
   await workflow.wait_condition(
       lambda: self._hitl_response is not None or self._cancelled,
       timeout=timedelta(days=7),
   )
   ```
5. Si **timeout** o `_cancelled`: activity `update_expense_to_rejected` con `reason="HITL_TIMEOUT"` (líneas 142–146) → flujo termina en sección 9.2.

---

## 9. Resolución HITL desde el backend → reanudación del workflow

### 9.1 Endpoint `POST /v1/hitl/{task_id}/resolve`

| Item | Valor |
|---|---|
| Archivo | `backend/src/nexus_backend/api/v1/hitl.py` |
| Función | `resolve_hitl()` |
| Líneas | 58 – 138 |
| Status | `204 No Content` |

Pasos:

1. **Validar tarea** (líneas 64–70) — busca en `mongo.hitl_tasks`, valida tenant, valida `status == "pending"`.
2. **Enviar signal a Temporal** (líneas 76–85):
   ```python
   await temporal_service.signal(
       workflow_id,
       "hitl_response",
       {
         "resolved_fields": body.resolved_fields,   # {amount?, vendor?, date?}
         "decision": body.decision,                 # "accept_ocr" | "keep_user_value" | "manual"
         "user_id": user.sub,
         "timestamp": now.isoformat(...),
       },
   )
   ```
3. **Marcar tarea resuelta** (líneas 87–98) — `update_one` sobre `hitl_tasks` setea `status="resolved"`, `decision`, `resolved_fields`, `resolved_by`, `resolved_at`.
4. **Timeline** (líneas 100–118) — inserta `expense_events` con `event_type="hitl_resolved"`.
5. **SSE** (líneas 122–136) — `sse_broker.publish_many(...)` con `workflow.hitl_resolved` a los canales user + workflow.

### 9.2 Continuación del workflow tras el signal

En `ExpenseAuditWorkflow.run` (líneas 154–208):

1. **`_apply_hitl_resolution(...)`** (líneas 284–308) — combina `resolved_fields` (override), OCR y user-reported según `decision`:
   - `accept_ocr` → toma valores OCR.
   - `keep_user_value` → toma valores del usuario.
   - `manual` / campo presente en `resolved_fields` → ese valor pisa todos.
2. **`_source_per_field(...)`** (líneas 311–328) — calcula un mapa `{amount: "hitl"|"ocr"|"user", ...}` para auditoría.
3. **`update_expense_to_approved`** (activity en `mongodb_writes.py:64-83`) — escribe:
   ```python
   $set: {
     "status": "approved",
     "final_amount", "final_vendor", "final_date", "final_currency",
     "source_per_field": {...},
     "approved_at", "updated_at"
   }
   ```
4. **`emit_expense_event("approved")`** (líneas 177–193).
5. **`publish_event("workflow.completed", payload={"final_state":"approved"})`** (líneas 198–208).

> **Camino sin discrepancias** (líneas 156–160): cuando `audit_result["has_discrepancies"] == False`, se salta el HITL y va directo a `update_expense_to_approved`, con `source_per_field = {field: "ocr_high_confidence" for field in final_data}`.

---

## 10. Eventos en tiempo real (Redis pub/sub → SSE)

### 10.1 Activity `publish_event`
| Item | Valor |
|---|---|
| Archivo | `nexus-orchestration/src/nexus_orchestration/activities/redis_events.py` |
| Función | `publish_event(event)` |
| Líneas | 60 – 91 |
| Cliente | `redis.from_url(settings.redis_url, ...)` |

Operaciones por evento (pipeline atómico):
```
PUBLISH    nexus:events:tenant:{t}:user:{u}            <payload>
PUBLISH    nexus:events:tenant:{t}:workflow:{wf}       <payload>
ZADD       <channel>:buffer  epoch_ms  <payload>
ZREMRANGEBYRANK <channel>:buffer 0 -51   # mantiene últimos 50
EXPIRE     <channel>:buffer 3600
```

### 10.2 Endpoint SSE
- `GET /workflows/{workflow_id}/stream` — abre un Server-Sent Events que replay-ea el buffer ordenado y luego sigue suscrito al canal pub/sub.
- Se cierra cuando llega `workflow.completed` o `workflow.failed`.

---

## 11. Tabla resumen de colecciones MongoDB

| Colección | Quién escribe | Cuándo | Campos clave |
|---|---|---|---|
| `receipts` | Backend `create_expense` | Al subir | `receipt_id`, `s3_bucket`, `s3_key`, `mime_type`, `size_bytes` |
| `expenses` | Backend (insert) + activities (`update_expense_status`, `update_expense_to_approved`, `update_expense_to_rejected`) | Estado del lifecycle | `status`, `final_*`, `source_per_field`, `workflow_id` |
| `expense_events` | Backend + activity `emit_expense_event` | Timeline append-only | `event_type` ∈ {`created`, `ocr_started`, `ocr_completed`, `discrepancy_detected`, `no_discrepancy`, `hitl_required`, `hitl_resolved`, `approved`, `rejected`} |
| `ocr_extractions` | Activity `upsert_ocr_extraction` | Tras Textract | `ocr_*`, `*_confidence`, `avg_confidence`, `textract_raw_s3_key` |
| `hitl_tasks` | Activity `create_hitl_task` + endpoint `resolve_hitl` | Cuando hay discrepancias | `status` ∈ {`pending`,`resolved`}, `discrepancy_fields`, `resolved_fields`, `decision` |

---

## 12. Tabla resumen de activities (por archivo)

| Archivo | Activities expuestas |
|---|---|
| `activities/textract.py` | `textract_analyze_expense`, `textract_analyze_document_queries` |
| `activities/mongodb_writes.py` | `update_expense_status`, `upsert_ocr_extraction`, `update_expense_to_approved`, `update_expense_to_rejected`, `create_hitl_task`, `emit_expense_event` |
| `activities/redis_events.py` | `publish_event` |
| `activities/query_expense.py` | `query_expense_for_validation` |
| `activities/comparison.py` | `compare_fields` |

---

## 13. Worker / task queues

| Task queue | Workflows | Activities | Worker |
|---|---|---|---|
| `nexus-orchestrator-tq` | `ExpenseAuditWorkflow` | `update_expense_status`, `update_expense_to_approved`, `update_expense_to_rejected`, `emit_expense_event`, `publish_event` | `nexus-orchestration/src/nexus_orchestration/main_worker.py` |
| `nexus-ocr-tq` | `OCRExtractionWorkflow` | `textract_analyze_expense`, `textract_analyze_document_queries`, `upsert_ocr_extraction`, `emit_expense_event`, `publish_event` | mismo proceso (worker poliglota) |
| `nexus-databricks-tq` | `AuditValidationWorkflow` | `query_expense_for_validation`, `compare_fields`, `create_hitl_task`, `emit_expense_event` | mismo proceso |

---

## 14. CDC hacia el lago Medallion (referencia)

Las escrituras a Mongo activan change streams que captura **Debezium** y publica a Kafka. La capa Bronze los ingiere a Delta:

- Bronze: `nexus-medallion/src/bronze/cdc_expenses.py` (`mongodb_cdc_expenses()`, líneas 65–110) consume `nexus.nexus_dev.expenses` desde Kafka.
- Silver: `nexus-medallion/src/silver/expenses.py` aplica limpieza, deduplicación por `__source_ts_ms` y proyecta a la tabla `silver.expenses`.

Esto es independiente del flujo de aprobación; el workflow no espera por el lake.

---

## 15. Estados terminales del workflow

| Estado final de `expenses.status` | Cómo se llega | Activity que lo escribe | Evento Redis | Evento timeline |
|---|---|---|---|---|
| `approved` (sin HITL) | OCR sin discrepancias | `update_expense_to_approved` | `workflow.completed` | `approved` |
| `approved` (post-HITL) | Humano resolvió la tarea | `update_expense_to_approved` (con `source_per_field` mezclado) | `workflow.completed` | `approved` |
| `rejected` | `wait_condition` expiró 7 días o `cancel_audit` | `update_expense_to_rejected` (reason `HITL_TIMEOUT` o `CANCELLED`) | `workflow.failed` | `rejected` |

El workflow retorna un dict con `{ "status": "approved" | "rejected" | "cancelled", "final_data": ..., "source_per_field": ... }` (líneas 196–210).

---

## 16. Mapa rápido de paths absolutos

```
backend/src/nexus_backend/
  api/v1/expenses.py                             # POST /expenses
  api/v1/hitl.py                                 # POST /hitl/{task_id}/resolve
  services/s3.py                                 # S3Service
  services/temporal_client.py                    # RealTemporalBackend
  services/sse_broker.py                         # SSEEventBroker (Redis)

nexus-orchestration/src/nexus_orchestration/
  main_worker.py                                 # registro de workflows + activities
  workflows/expense_audit.py                     # ExpenseAuditWorkflow (orquestador)
  workflows/ocr_extraction.py                    # OCRExtractionWorkflow
  workflows/audit_validation.py                  # AuditValidationWorkflow
  activities/textract.py                         # OCR (AWS Textract)
  activities/mongodb_writes.py                   # writes a Mongo + emit_expense_event + create_hitl_task
  activities/redis_events.py                     # publish_event
  activities/query_expense.py                    # query_expense_for_validation
  activities/comparison.py                       # compare_fields
```

---

## 17. Notas de exactitud

- Las líneas se citan con el repositorio a la fecha de auditoría. Si renombras un activity o reordenas el workflow, **revisa secciones 5–7 antes de pasar este doc a producción** (los nombres son estables; los números no).
- El flujo descrito es **el implementado en código**, no el documentado en `03-orquestacion-temporal.md`. Donde difieran, la implementación gana — este documento refleja la implementación.
- El campo `source_per_field` es el mecanismo de auditoría que permite, leyendo solo `expenses`, reconstruir si cada valor final vino del OCR, del usuario o de un humano (`hitl`).
