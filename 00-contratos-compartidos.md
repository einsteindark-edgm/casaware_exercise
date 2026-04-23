# 00 · Contratos Compartidos y Arquitectura Global — Nexus

> **Propósito de este documento.** Este es el documento **fuente de verdad** que los cinco sistemas (Frontend, Backend, Exoclaw+Temporal, CDC, Medallion) deben respetar para que coordinen entre sí. Cualquier cambio aquí obliga a revisar los cinco documentos restantes.

---

## 1. Visión general del flujo end-to-end

```
┌─────────────┐  1. Login   ┌──────────────┐
│   Frontend  │────────────▶│ AWS Cognito  │──┐
│ (Next.js)   │◀────JWT─────│  User Pool   │  │
└──────┬──────┘             └──────────────┘  │ tenant_id
       │                                       │ en custom claims
       │ 2. Upload recibo + formulario         │
       ▼                                       │
┌─────────────┐              ┌──────────┐     │
│   FastAPI   │──3. Guarda──▶│ MongoDB  │     │
│   Backend   │──    S3   ──▶│    +     │     │
└──────┬──────┘              │    S3    │     │
       │                     └────┬─────┘     │
       │ 4. Start Workflow        │           │
       ▼                          │ 5. CDC    │
┌──────────────┐                  ▼           │
│  Temporal    │            ┌──────────────┐  │
│  + Exoclaw   │            │  Debezium +  │  │
│  (Workers)   │            │    Kafka     │  │
└──────┬───────┘            └──────┬───────┘  │
       │ 6. Invoca Textract         │ 7. Sink │
       ▼                            ▼         │
┌──────────────┐            ┌──────────────────────────────┐
│ AWS Textract │            │   Databricks Lakehouse       │
│              │            │   Bronze → Silver → Gold     │
└──────┬───────┘            │   Unity Catalog + Vector S.  │
       │                    └──────────────────────────────┘
       │ 8. Resultado JSON            ▲
       └──────────────────────────────┘
       │ 9. Discrepancia → Signal HITL
       ▼
┌──────────────┐  10. Publish  ┌──────────┐
│   Worker     │─────────────▶│  Redis   │
│   Temporal   │               │  Pub/Sub │
└──────────────┘               └────┬─────┘
                                    │ 11. SSE subscription
                                    ▼
                              ┌─────────────┐
                              │   FastAPI   │──SSE──▶ Frontend
                              │             │        (reactive UI)
                              └─────────────┘
                                    │
                                    │ 12. Usuario corrige
                                    ▼
                              Signal Temporal → Workflow resume
```

---

## 2. Contratos compartidos — referencia obligatoria

### 2.1. JWT de AWS Cognito (custom claims)

Cognito User Pool emite ID Tokens con los siguientes claims. **Todos los sistemas validan el token contra el JWKS público del pool**.

| Claim | Tipo | Descripción | Ejemplo |
|---|---|---|---|
| `sub` | string (UUID) | ID único del usuario | `a3f4...` |
| `email` | string | Email verificado | `juan@acme.co` |
| `custom:tenant_id` | string (UUID) | ID del tenant | `t_01HQ...` |
| `custom:role` | string | `admin`, `auditor`, `user` | `user` |
| `cognito:groups` | string[] | Grupos Cognito | `["nexus-users"]` |
| `token_use` | string | Siempre `id` para ID tokens | `id` |
| `exp` | int | Unix epoch expiración | `1735689600` |

**Issuer esperado:** `https://cognito-idp.{AWS_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}`
**JWKS URI:** `{issuer}/.well-known/jwks.json`
**Algoritmo:** `RS256`

### 2.2. Canales Redis Pub/Sub

Redis actúa como bus de eventos entre los Workers de Temporal y FastAPI. **Nombres exactos, case-sensitive**:

```
nexus:events:tenant:{tenant_id}:user:{user_id}
    └─ Canal personal del usuario. Recibe todos los eventos que le conciernen.

nexus:events:tenant:{tenant_id}:workflow:{workflow_id}
    └─ Canal del workflow específico. Útil cuando el frontend se suscribe a un workflow concreto.

nexus:events:global:system
    └─ Eventos de sistema (mantenimiento, etc.). Opcional para v1.
```

**Estrategia de fan-out.** Los Workers publican SIEMPRE en los dos canales relevantes (user + workflow) para dar flexibilidad al cliente.

### 2.3. Esquema de Evento (Redis → SSE)

Todos los eventos son JSON con esta estructura canónica:

```json
{
  "schema_version": "1.0",
  "event_id": "evt_01HQ...",
  "event_type": "workflow.started | workflow.ocr_progress | workflow.hitl_required | workflow.completed | workflow.failed | chat.token | chat.complete",
  "workflow_id": "expense-audit-01HQ...",
  "tenant_id": "t_01HQ...",
  "user_id": "a3f4...",
  "expense_id": "exp_01HQ...",
  "timestamp": "2026-04-22T15:30:00.000Z",
  "payload": {
    "...": "específico por event_type"
  }
}
```

**Tipos de evento y payloads esperados:**

| `event_type` | Cuándo se emite | Payload clave |
|---|---|---|
| `workflow.started` | Al iniciar el parent workflow | `{ "expense_id": "...", "status": "running" }` |
| `workflow.ocr_progress` | Durante la actividad de Textract | `{ "step": "textract_call" \| "parsing" \| "validating", "progress_pct": 0-100 }` |
| `workflow.hitl_required` | Al detectarse discrepancia | `{ "hitl_task_id": "...", "fields_in_conflict": [{"field":"amount","user_value":100,"ocr_value":100.5,"confidence":95.2}] }` |
| `workflow.completed` | Fin exitoso | `{ "final_state": "approved" }` (la promoción a Gold ocurre asíncrona vía Lakeflow, no se referencia aquí) |
| `workflow.failed` | Error no recuperable | `{ "error_code": "TEXTRACT_UNAVAILABLE", "message": "..." }` |
| `chat.token` | Token a token en RAG streaming | `{ "token": "El", "session_id": "..." }` |
| `chat.complete` | Fin de respuesta RAG | `{ "session_id": "...", "citations": [...] }` |

### 2.4. Workflows, Signals y Queries de Temporal

**Namespace de Temporal:** `nexus-{env}` (ej: `nexus-prod`, `nexus-dev`).

**Task Queues:**
- `nexus-orchestrator-tq` — workers que ejecutan workflows
- `nexus-ocr-tq` — workers dedicados a actividades de Textract (I/O pesado)
- `nexus-databricks-tq` — workers que llaman a Databricks SQL / Vector Search
- `nexus-rag-tq` — workers para el chatbot RAG

**Workflows:**

| Nombre | Tipo | `workflow_id` format | Descripción |
|---|---|---|---|
| `ExpenseAuditWorkflow` | Parent | `expense-audit-{expense_id}` | Orquesta todo el proceso de auditoría de un gasto |
| `OCRExtractionWorkflow` | Child | `ocr-{expense_id}` | Agente Exoclaw que extrae datos con Textract |
| `AuditValidationWorkflow` | Child | `audit-{expense_id}` | Agente Exoclaw que compara OCR vs MongoDB |
| `RAGQueryWorkflow` | Standalone | `rag-{session_id}-{turn_n}` | Responde consulta RAG del chatbot |

**Signals** (enviados por FastAPI al Worker):

| Signal Name | Destino | Payload |
|---|---|---|
| `hitl_response` | `ExpenseAuditWorkflow` | `{ "resolved_fields": {"amount": 100.5}, "user_id": "...", "decision": "accept_ocr" \| "keep_user_value" \| "custom", "timestamp": "..." }` |
| `cancel_audit` | `ExpenseAuditWorkflow` | `{ "reason": "...", "user_id": "..." }` |
| `user_message` | `RAGQueryWorkflow` | `{ "message": "¿Cuánto gasté en marzo?", "message_id": "..." }` |

**Queries** (FastAPI → Worker, síncronas, read-only):

| Query Name | Return Type | Descripción |
|---|---|---|
| `get_status` | `{ "state": "running|waiting_hitl|completed|failed", "current_step": "..." }` | Estado actual |
| `get_hitl_data` | `{ "task_id": "...", "fields_in_conflict": [...] } \| null` | Datos HITL pendientes |
| `get_history` | `[{ "step": "...", "timestamp": "...", "result": "..." }]` | Historial para debugging |

### 2.5. Namespace de Unity Catalog

**Catálogo:** `nexus_{env}` (ej: `nexus_prod`)
**Esquemas:**

```
nexus_prod/
├── bronze/                       # Datos crudos — append-only
│   ├── mongodb_cdc_expenses      # Eventos CDC de MongoDB (Debezium)
│   ├── mongodb_cdc_receipts
│   ├── mongodb_cdc_hitl_tasks
│   └── textract_raw_output       # JSON completo de Textract por recibo
│
├── silver/                       # Datos limpios y normalizados
│   ├── expenses                  # Gastos (replica viva de MongoDB vía APPLY CHANGES)
│   ├── receipts
│   ├── ocr_extractions           # Campos OCR + confidence scores
│   └── hitl_events               # Log histórico de intervenciones humanas
│
├── gold/                         # Datos listos para negocio y RAG
│   ├── expense_audit             # Un registro final por gasto auditado
│   ├── expense_chunks            # Chunks de texto + metadata para RAG
│   └── kpi_tenant_daily          # Agregados para dashboards
│
└── vector/                       # Índices de Vector Search
    └── expense_chunks_index      # Delta Sync Index sobre gold.expense_chunks
```

**Regla de seguridad transversal.** Toda tabla tiene columna `tenant_id STRING NOT NULL` y una Row-Level Filter function aplicada vía Unity Catalog:

```sql
CREATE FUNCTION nexus_prod.security.tenant_filter(tenant_id STRING)
RETURN IF(
  is_account_group_member('nexus-admins'),
  true,
  tenant_id = current_user_tenant()
);
```

### 2.6. MongoDB — Colecciones y campos críticos

Base de datos: `nexus_{env}`. Las siguientes colecciones son **observadas por Debezium** y replicadas a Bronze:

```javascript
// expenses
{
  _id: ObjectId,
  expense_id: "exp_01HQ...",    // Primary identifier across systems
  tenant_id: "t_01HQ...",
  user_id: "a3f4...",
  amount: 100.50,
  currency: "COP",
  date: ISODate("2026-04-22"),
  vendor: "Starbucks",
  category: "food",
  receipt_id: "rcpt_01HQ...",   // FK a receipts
  workflow_id: "expense-audit-exp_01HQ...",  // FK a Temporal
  status: "pending" | "processing" | "hitl_required" | "approved" | "rejected",
  created_at: ISODate(),
  updated_at: ISODate()
}

// receipts
{
  _id: ObjectId,
  receipt_id: "rcpt_01HQ...",
  tenant_id: "t_01HQ...",
  user_id: "a3f4...",
  s3_bucket: "nexus-receipts-prod",
  s3_key: "tenant=t_01HQ.../user=a3f4.../receipt_01HQ.pdf",
  mime_type: "application/pdf",
  size_bytes: 123456,
  uploaded_at: ISODate()
}

// hitl_tasks
{
  _id: ObjectId,
  task_id: "hitl_01HQ...",
  workflow_id: "expense-audit-...",
  tenant_id: "t_01HQ...",
  user_id: "a3f4...",
  expense_id: "exp_01HQ...",
  fields_in_conflict: [
    { field: "amount", user_value: 100, ocr_value: 100.5, confidence: 95.2 }
  ],
  status: "pending" | "resolved" | "cancelled",
  created_at: ISODate(),
  resolved_at: ISODate?
}

// ocr_extractions
// Resultado normalizado del OCR. El Worker lo escribe tras invocar Textract.
// Una sola fila por expense_id (upsert). Reemplaza completamente al "insert directo
// en Gold" que tenía el diseño anterior. CDC lo replica a silver.ocr_extractions.
{
  _id: ObjectId,
  expense_id: "exp_01HQ...",       // PK lógica + key del CDC
  tenant_id: "t_01HQ...",
  user_id: "a3f4...",
  // Campos extraídos por Textract, normalizados
  ocr_total: 100.50,
  ocr_total_confidence: 99.85,
  ocr_vendor: "Starbucks Reserve",
  ocr_vendor_confidence: 96.12,
  ocr_date: ISODate("2026-04-22"),
  ocr_date_confidence: 91.40,
  ocr_currency: "COP",
  avg_confidence: 95.79,
  // Trazabilidad
  textract_raw_s3_key: "tenant=t_01HQ.../expense=exp_01HQ.../{ulid}.json",
  extracted_by_workflow_id: "ocr-exp_01HQ...",
  extracted_at: ISODate()
}

// expense_events
// Timeline semántico del gasto. El Worker (y el Backend en algunos casos) inserta
// un evento por cada paso significativo. Append-only: nunca se updatea ni borra.
// Es la fuente de verdad para la pantalla "Detalle de gasto / Historial" en el frontend.
// Distinto de bronze.mongodb_cdc_*: bronze es el dump técnico de cada cambio de campo;
// expense_events es de alto nivel y orientado al usuario.
{
  _id: ObjectId,
  event_id: "evt_01HQ...",         // ULID, ordenable
  expense_id: "exp_01HQ...",
  tenant_id: "t_01HQ...",
  event_type: "created" | "ocr_started" | "ocr_completed" | "ocr_failed"
            | "audit_started" | "discrepancy_detected" | "no_discrepancy"
            | "hitl_required" | "hitl_resolved" | "hitl_cancelled"
            | "approved" | "rejected" | "failed",
  actor: { type: "user" | "system" | "agent",
           id: "a3f4..." | "worker:nexus-orchestrator-tq" | "agent:ocr_extraction" },
  details: { ... },                // específico por event_type, ver tabla abajo
  workflow_id: "expense-audit-...",
  created_at: ISODate()
}
```

**Convención de `details` por `event_type`:**

| `event_type` | `details` payload |
|---|---|
| `created` | `{ user_reported: { amount, vendor, date, ... } }` |
| `ocr_started` | `{ s3_key }` |
| `ocr_completed` | `{ avg_confidence, fields_summary: [{field, value, confidence}] }` |
| `ocr_failed` | `{ error_code, error_message, retry_count }` |
| `discrepancy_detected` | `{ fields_in_conflict: [{field, user_value, ocr_value, confidence}], hitl_task_id }` |
| `hitl_resolved` | `{ decision, resolved_fields, hitl_task_id }` |
| `approved` | `{ final_data: {amount, vendor, date}, source_per_field: {amount: "ocr"|"user"|"hitl", ...} }` |
| `rejected` / `failed` | `{ reason }` |

### 2.7. S3 Buckets

| Bucket | Contenido | Ciclo de vida |
|---|---|---|
| `nexus-receipts-{env}` | Imágenes y PDFs originales subidos por usuarios | Glacier tras 90 días |
| `nexus-textract-output-{env}` | JSON crudo de Textract (backup) | Standard-IA tras 30 días |
| `nexus-cdc-sink-{env}` | Sink opcional de Kafka Connect si no se usa Kafka directamente a Databricks | Expira 7 días |

**Partition scheme** en recibos: `tenant={tenant_id}/user={user_id}/{ulid}.{ext}`.

### 2.8. Identificadores y formato

- **ULIDs** para todos los IDs de negocio (`exp_`, `rcpt_`, `hitl_`, `evt_`). Motivo: ordenables lexicográficamente, compatibles con índices.
- **Prefijos obligatorios** para debugging: `exp_`, `rcpt_`, `hitl_`, `evt_`, `t_` (tenant).
- **Timestamps** siempre en ISO 8601 UTC (`2026-04-22T15:30:00.000Z`).

---

## 3. Matriz de variables de entorno globales

Estas variables existen **en todos los sistemas** (ajustar solo prefijo si aplica):

```bash
# Entorno
ENV=dev|staging|prod
AWS_REGION=us-east-1

# Cognito
COGNITO_USER_POOL_ID=us-east-1_XXXXX
COGNITO_APP_CLIENT_ID=xxxxx
COGNITO_JWKS_URL=https://cognito-idp.us-east-1.amazonaws.com/us-east-1_XXXXX/.well-known/jwks.json

# Temporal
TEMPORAL_HOST=nexus.tmprl.cloud:7233
TEMPORAL_NAMESPACE=nexus-prod
TEMPORAL_TLS_CERT_PATH=/secrets/temporal.pem
TEMPORAL_TLS_KEY_PATH=/secrets/temporal.key

# Redis
REDIS_URL=redis://:PASSWORD@nexus-redis.cache.amazonaws.com:6379/0
REDIS_TLS=true

# MongoDB
MONGODB_URI=mongodb+srv://user:pass@cluster.mongodb.net/nexus_prod
MONGODB_DB=nexus_prod

# AWS services
S3_RECEIPTS_BUCKET=nexus-receipts-prod
S3_TEXTRACT_OUTPUT_BUCKET=nexus-textract-output-prod

# Databricks
DATABRICKS_HOST=https://adb-xxxx.azuredatabricks.net
DATABRICKS_TOKEN=dapi-...            # Service principal recomendado en prod
DATABRICKS_WAREHOUSE_ID=abc123
DATABRICKS_CATALOG=nexus_prod
DATABRICKS_VS_ENDPOINT=nexus-vs-endpoint
DATABRICKS_VS_INDEX=nexus_prod.vector.expense_chunks_index

# LLM
ANTHROPIC_API_KEY=sk-ant-...         # Para Claude via Exoclaw
LLM_MODEL=claude-sonnet-4-6          # O claude-opus-4-7
```

---

## 4. Separación de responsabilidades — invariante arquitectónica

> Esta sección codifica una decisión que toca al Worker, al medallion y al frontend simultáneamente. Si entiendes esto, entiendes el sistema.

**MongoDB es la única fuente de verdad transaccional.** Todos los datos operacionales viven aquí. Cualquier sistema que necesite leer estado actual del negocio debería leer de Mongo (vía el Backend, no directamente).

**El Worker (Temporal) solo escribe a MongoDB.** Nunca escribe a Databricks. Sus responsabilidades son: orquestar el ciclo de vida del expense, invocar Textract, decidir si hay discrepancia, esperar el HITL, y al final actualizar el `status` del expense en Mongo. Eso es todo.

**El medallion materializa, no orquesta.** Bronze recibe los CDC events de Mongo. Silver normaliza y deduplica con APPLY CHANGES INTO. Una pipeline Lakeflow detecta cuando un expense alcanza `status='approved'` y lo promueve a Gold. Gold alimenta Vector Search. Nada de esto requiere conocimiento del Worker — Lakeflow vive en Databricks y reacciona a cambios de datos, no a comandos.

**El frontend nunca consulta a Databricks.** Toda lectura va al Backend, que consulta MongoDB. La única excepción son los chunks recuperados por el agente RAG durante una conversación, y eso lo hace el Worker, no el frontend.

**Consecuencia 1 (UX):** la pantalla de detalle de un expense es rápida (Mongo ~10ms) en vez de lenta (Databricks SQL ~2-5s).

**Consecuencia 2 (reprocesamiento):** si mañana cambia la lógica de cómo se construye Gold (ej: cambiar la regla de coalescencia de campos), basta truncar Gold y dejar que Lakeflow lo reconstruya desde Silver. Cero downtime, cero workflows re-ejecutados.

**Consecuencia 3 (auditoría):** la colección `expense_events` (ver §2.6) provee un timeline de alto nivel para el usuario. Bronze provee el dump técnico granular para auditoría forense. Cada uno sirve a su consumidor.

---

## 5. Regla de oro para desarrolladores

> Si necesitas cambiar el nombre de un canal, una tabla, un signal o el schema de un evento: **actualiza este documento primero**, luego ajusta los cinco documentos de sistema. De lo contrario, los sistemas se desincronizan silenciosamente.

---

## 6. Orden recomendado de desarrollo

1. **CDC + Medallion** en paralelo con mocks de datos (no dependen del resto).
2. **Backend (FastAPI)** con mocks de Temporal y Databricks.
3. **Exoclaw + Temporal** cuando el backend tenga los endpoints de signal y query listos.
4. **Frontend** al final, consumiendo el backend real.

Cada sistema tiene en su documento una sección de **mocks** y **datos de prueba** para desarrollo aislado.
