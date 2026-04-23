# 03 · Orquestación — Exoclaw + Temporal Workers

> **Rol en Nexus.** Este sistema ejecuta los **agentes duraderos** que orquestan la auditoría de cada gasto y las consultas del chatbot RAG. Combina Temporal.io (durabilidad, signals, retries) con Exoclaw (loop agéntico con LLM + tools). Es el único sistema que llama a Textract, Databricks SQL y Vector Search; el backend nunca lo hace directamente.
>
> **Pre-requisito de lectura:** [`00-contratos-compartidos.md`](./00-contratos-compartidos.md). Los contratos de workflows, signals, canales Redis y schemas vienen de ahí.

---

## 1. Decisión arquitectónica: por qué Exoclaw + Temporal

- **Temporal** aporta durabilidad: un workflow que se pausa 7 días esperando una decisión humana (HITL) resume exactamente donde quedó, sobreviviendo a despliegues y caídas de workers. Las invocaciones a Textract, Databricks, y LLM se modelan como `activities` con retry policy declarativa.
- **Exoclaw** aporta el *agent loop*: LLM ↔ tools con un protocolo mínimo (~2000 líneas). Su `Executor` protocol permite delegar cada operación del loop a Temporal, obteniendo un agente donde **cada paso es un activity checkpoint**. Esto significa que si un tool falla o el worker se reinicia, el agente **no reinicia la conversación desde cero**.
- La arquitectura es **jerárquica**: un `ExpenseAuditWorkflow` (parent) coordina `OCRExtractionWorkflow` (child agente) y `AuditValidationWorkflow` (child agente). Cada child es un Exoclaw `AgentLoop` corriendo bajo un `TemporalExecutor`.

**Referencias clave:**
- Exoclaw: https://github.com/Clause-Logic/exoclaw
- `exoclaw-executor-temporal`: paquete Python que provee el `Executor` que corre operaciones como activities Temporal.
- Temporal AI cookbook: https://docs.temporal.io/ai-cookbook

---

## 2. Stack técnico

| Componente | Librería / Versión | Notas |
|---|---|---|
| Workflow engine | **temporalio ≥ 1.8** | SDK Python oficial |
| Agent framework | **exoclaw** (PyPI) + **exoclaw-executor-temporal** | Core + ejecutor Temporal |
| LLM provider | **exoclaw-provider-litellm** o **-anthropic** | LiteLLM para multi-modelo, anthropic-sdk directo para Claude |
| Conversation storage | **exoclaw-conversation-redis** | Sesiones multi-worker |
| Redis | **redis-py 5.0+** (async) | Publish a canales del contrato |
| AWS Textract | **boto3 1.34+** | `analyze_expense` síncrono, `start_expense_analysis` async |
| Databricks SQL | **databricks-sql-connector 3.3+** | Ejecuta queries contra Silver/Gold |
| Databricks Vector Search | **databricks-vectorsearch 0.50+** | Cliente oficial |
| MongoDB | **pymongo 4.8+** (sync dentro de activities) | Queries de validación |
| Observabilidad | **opentelemetry-sdk**, **structlog**, **aws-xray-sdk** | Traces propagados desde FastAPI |

**Python:** 3.12+.

---

## 3. Estructura del proyecto

```
nexus-orchestration/
├── pyproject.toml
├── uv.lock
├── Dockerfile
├── src/
│   └── nexus_orchestration/
│       ├── __init__.py
│       ├── config.py
│       ├── main_worker.py               # Entry de workers (un proceso por task queue)
│       │
│       ├── workflows/
│       │   ├── __init__.py
│       │   ├── expense_audit.py         # ExpenseAuditWorkflow (parent)
│       │   ├── ocr_extraction.py        # OCRExtractionWorkflow (child, agente)
│       │   ├── audit_validation.py      # AuditValidationWorkflow (child, agente)
│       │   └── rag_query.py             # RAGQueryWorkflow (agente chat)
│       │
│       ├── activities/
│       │   ├── __init__.py
│       │   ├── textract.py              # call_textract_analyze_expense
│       │   ├── databricks_sql.py        # query_silver (read-only)
│       │   ├── mongodb_writes.py        # upsert_ocr_extraction, update_expense_to_approved, emit_expense_event
│       │   ├── vector_search.py         # similarity_search
│       │   ├── mongodb.py               # get_expense, update_status, create_hitl_task
│       │   ├── redis_events.py          # publish_event (respeta schema del contrato)
│       │   └── llm.py                   # Invocación de LLM (cuando no se usa Exoclaw)
│       │
│       ├── agents/
│       │   ├── __init__.py
│       │   ├── exoclaw_factory.py       # Construye Exoclaw con TemporalExecutor
│       │   ├── tools/
│       │   │   ├── textract_tool.py
│       │   │   ├── databricks_query_tool.py
│       │   │   ├── vector_search_tool.py
│       │   │   ├── mongodb_lookup_tool.py
│       │   │   └── publish_event_tool.py
│       │   └── prompts/
│       │       ├── ocr_extraction.md    # System prompt del agente OCR
│       │       ├── audit_validation.md  # System prompt del auditor
│       │       └── rag_answer.md        # System prompt del chatbot
│       │
│       ├── schemas/
│       │   ├── inputs.py                # Inputs tipados de cada workflow
│       │   └── events.py                # EventEnvelope (idéntico al de backend)
│       │
│       └── observability/
│           ├── logging.py
│           └── tracing.py
└── tests/
    ├── unit/
    ├── workflows/                       # Usa WorkflowEnvironment de Temporal
    └── integration/
```

**Regla de oro de Temporal:** TODO lo que no sea determinista (tiempo, I/O, random) **debe** ir en un activity. Los workflows son puro orquestador.

---

## 4. Workflow orquestador: `ExpenseAuditWorkflow`

```python
# src/nexus_orchestration/workflows/expense_audit.py
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy
from ..schemas.inputs import ExpenseAuditInput, HitlResponse
from .ocr_extraction import OCRExtractionWorkflow
from .audit_validation import AuditValidationWorkflow

@workflow.defn(name="ExpenseAuditWorkflow")
class ExpenseAuditWorkflow:
    def __init__(self) -> None:
        self._state: str = "pending"
        self._current_step: str = "initializing"
        self._hitl_response: HitlResponse | None = None
        self._hitl_task_id: str | None = None
        self._cancelled: bool = False

    @workflow.run
    async def run(self, inp: ExpenseAuditInput) -> dict:
        # 1) Publicar evento workflow.started
        await workflow.execute_activity(
            "publish_event",
            {
                "event_type": "workflow.started",
                "tenant_id": inp.tenant_id,
                "user_id": inp.user_id,
                "expense_id": inp.expense_id,
                "payload": {"workflow_id": workflow.info().workflow_id},
            },
            start_to_close_timeout=timedelta(seconds=5),
        )

        # 2) Child: OCR con agente Exoclaw
        self._current_step = "ocr_extraction"
        ocr_result = await workflow.execute_child_workflow(
            OCRExtractionWorkflow.run,
            inp,
            id=f"ocr-{inp.expense_id}",
            task_queue="nexus-ocr-tq",
            retry_policy=RetryPolicy(
                maximum_attempts=3,
                non_retryable_error_types=["UnsupportedMimeTypeError"],
            ),
        )

        # 3) Child: Auditoría (agente compara OCR vs MongoDB)
        self._current_step = "audit_validation"
        audit_result = await workflow.execute_child_workflow(
            AuditValidationWorkflow.run,
            {"expense_input": inp, "ocr_result": ocr_result},
            id=f"audit-{inp.expense_id}",
            task_queue="nexus-databricks-tq",
        )

        # 4) Si hay discrepancias → HITL
        if audit_result["has_discrepancies"]:
            self._state = "waiting_hitl"
            self._current_step = "waiting_human"
            self._hitl_task_id = audit_result["hitl_task_id"]

            await workflow.execute_activity(
                "publish_event",
                {
                    "event_type": "workflow.hitl_required",
                    "tenant_id": inp.tenant_id,
                    "user_id": inp.user_id,
                    "expense_id": inp.expense_id,
                    "payload": {
                        "hitl_task_id": self._hitl_task_id,
                        "fields_in_conflict": audit_result["fields_in_conflict"],
                    },
                },
                start_to_close_timeout=timedelta(seconds=5),
            )

            # Esperar hasta 7 días por signal del usuario o cancelación
            try:
                await workflow.wait_condition(
                    lambda: self._hitl_response is not None or self._cancelled,
                    timeout=timedelta(days=7),
                )
            except TimeoutError:
                await self._publish_failed(inp, "HITL_TIMEOUT", "Timeout de 7 días sin respuesta")
                raise

            if self._cancelled:
                await self._publish_failed(inp, "CANCELLED", "Cancelado por el usuario")
                return {"status": "cancelled"}

            # Aplicar la resolución
            final_data = self._apply_hitl_resolution(audit_result, self._hitl_response)
        else:
            final_data = audit_result["extracted_data"]

        # 5) Aplicar resolución (HITL o auto si no hubo discrepancia) y persistir en Mongo
        self._current_step = "finalizing_in_mongo"

        if audit_result["has_discrepancies"]:
            final_data = self._apply_hitl_resolution(audit_result, self._hitl_response)
            source_per_field = self._compute_source_per_field(
                audit_result, self._hitl_response
            )
        else:
            final_data = audit_result["extracted_data"]
            source_per_field = {f: "ocr_high_confidence" for f in final_data}

        # IMPORTANTE: el Worker NO escribe a Databricks. Solo actualiza Mongo.
        # El medallion (Lakeflow) detectará el cambio de status vía CDC y promoverá a Gold.
        await workflow.execute_activity(
            "update_expense_to_approved",
            {
                "tenant_id": inp.tenant_id,
                "expense_id": inp.expense_id,
                "final_data": final_data,
                "source_per_field": source_per_field,
            },
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )

        # 6) Emitir el evento semántico "approved" para el timeline del frontend
        await workflow.execute_activity(
            "emit_expense_event",
            {
                "expense_id": inp.expense_id,
                "tenant_id": inp.tenant_id,
                "event_type": "approved",
                "actor": {"type": "system", "id": "worker:nexus-orchestrator-tq"},
                "details": {
                    "final_data": final_data,
                    "source_per_field": source_per_field,
                },
            },
            start_to_close_timeout=timedelta(seconds=5),
        )

        # 7) Notificar al frontend por SSE
        self._state = "completed"
        await workflow.execute_activity(
            "publish_event",
            {
                "event_type": "workflow.completed",
                "tenant_id": inp.tenant_id,
                "user_id": inp.user_id,
                "expense_id": inp.expense_id,
                "payload": {"final_state": "approved"},
            },
            start_to_close_timeout=timedelta(seconds=5),
        )

        return {"status": "completed", "final_data": final_data}

    # --- Signals ---
    @workflow.signal(name="hitl_response")
    async def hitl_response(self, response: HitlResponse) -> None:
        self._hitl_response = response

    @workflow.signal(name="cancel_audit")
    async def cancel_audit(self, payload: dict) -> None:
        self._cancelled = True

    # --- Queries ---
    @workflow.query(name="get_status")
    def get_status(self) -> dict:
        return {"state": self._state, "current_step": self._current_step}

    @workflow.query(name="get_hitl_data")
    def get_hitl_data(self) -> dict | None:
        if self._hitl_task_id is None:
            return None
        return {"task_id": self._hitl_task_id}
```

**Puntos clave:**

- `workflow.wait_condition` + timeout de 7 días: el workflow permanece en memoria mínima durante la espera, Temporal persiste el estado.
- Los signals mutan state atómicamente dentro del workflow (Temporal serializa signals).
- Las queries son `def` (no `async`), read-only, no pueden ejecutar activities.

---

## 5. Child workflow agéntico: `OCRExtractionWorkflow`

Este workflow ES un agente Exoclaw ejecutándose dentro de Temporal. El agente decide, turno a turno, qué tool invocar:

1. Llamar Textract (`textract_tool`).
2. Si el Confidence promedio es bajo, re-intentar con otra API de Textract (e.g., `AnalyzeDocument` con `QUERIES` apuntando a campos específicos).
3. Publicar progreso (`publish_event_tool`).
4. Devolver campos extraídos cuando esté satisfecho.

### 5.1. Factory del agente Exoclaw con TemporalExecutor

```python
# src/nexus_orchestration/agents/exoclaw_factory.py
from exoclaw import Exoclaw
from exoclaw.agent.loop import AgentLoop
from exoclaw.bus.queue import MessageBus
from exoclaw_executor_temporal import TemporalExecutor  # hipotético nombre del paquete
from exoclaw_provider_anthropic import AnthropicProvider
from exoclaw_conversation_redis import RedisConversation

from ..agents.tools.textract_tool import TextractTool
from ..agents.tools.publish_event_tool import PublishEventTool

def build_ocr_agent(
    *,
    tenant_id: str,
    user_id: str,
    expense_id: str,
    redis_url: str,
    model: str = "claude-sonnet-4-6",
) -> Exoclaw:
    """Construye un Exoclaw configurado para ejecutarse dentro de un Temporal workflow.

    CRÍTICO: TemporalExecutor envuelve cada operación del loop (chat, execute_tool, build_prompt)
    como una activity Temporal, garantizando que cada llamada sea replay-safe."""
    provider = AnthropicProvider(default_model=model)
    conversation = RedisConversation(
        url=redis_url,
        session_prefix=f"exoclaw:sessions:ocr:{tenant_id}",
    )
    executor = TemporalExecutor(
        default_start_to_close_timeout_seconds=60,
        chat_retry_policy={"maximum_attempts": 3},
        tool_retry_policy={"maximum_attempts": 2},
    )
    with open("src/nexus_orchestration/agents/prompts/ocr_extraction.md") as f:
        system_prompt = f.read()

    return Exoclaw(
        provider=provider,
        conversation=conversation,
        executor=executor,
        system_prompt_template=system_prompt,
        tools=[
            TextractTool(expense_id=expense_id, tenant_id=tenant_id),
            PublishEventTool(tenant_id=tenant_id, user_id=user_id, expense_id=expense_id),
        ],
        max_iterations=8,
    )
```

### 5.2. El workflow que lo ejecuta

```python
@workflow.defn(name="OCRExtractionWorkflow")
class OCRExtractionWorkflow:
    @workflow.run
    async def run(self, inp: ExpenseAuditInput) -> dict:
        # Nota: construir el agente dentro del workflow está OK porque
        # TemporalExecutor sólo proxyea operaciones a activities; el constructor
        # no hace I/O.
        agent = build_ocr_agent(
            tenant_id=inp.tenant_id,
            user_id=inp.user_id,
            expense_id=inp.expense_id,
            redis_url=workflow.memo_value("redis_url"),
        )

        user_message = (
            f"Extrae los campos financieros del recibo almacenado en S3 key "
            f"`{inp.receipt_s3_key}`. Reporta cada campo con su confidence score. "
            f"Si la confianza media es menor al 80%, intenta re-extraer con queries específicas."
        )

        # process_direct es el punto de entrada síncrono del agente
        result = await agent.process_direct(
            session_id=f"ocr-{inp.expense_id}",
            message=user_message,
        )

        # result.final_output es un JSON con los campos extraídos
        return json.loads(result.final_output)
```

### 5.3. System prompt del agente OCR (`ocr_extraction.md`)

```markdown
Eres un agente de extracción de datos financieros para Nexus.

Tu trabajo: extraer de un recibo (almacenado en S3) los siguientes campos:
- amount (decimal)
- currency (ISO 4217)
- date (ISO 8601)
- vendor (string)
- tax_id (si está presente)

Usa SIEMPRE la herramienta `textract_analyze_expense` primero. Si el Confidence
promedio de los campos clave (amount, date) es menor a 80%, intenta una vez más
usando `textract_analyze_document_queries` con queries explícitas.

Publica progreso con `publish_event` al inicio y cuando obtengas resultados.

Formato de salida final (último mensaje, JSON válido):
{
  "fields": {
    "amount": {"value": 100.50, "confidence": 99.2},
    "currency": {"value": "COP", "confidence": 85.0},
    ...
  },
  "avg_confidence": 92.3,
  "raw_textract_ref": "s3://nexus-textract-output-prod/expense=exp_...json"
}
```

---

## 6. Activities — implementación de referencia

### 6.1. `textract_analyze_expense`

```python
# src/nexus_orchestration/activities/textract.py
import boto3
import json
from temporalio import activity
from ..config import settings

@activity.defn(name="textract_analyze_expense")
async def textract_analyze_expense(inp: dict) -> dict:
    """Llama AnalyzeExpense síncrono.

    inp = {"s3_bucket": "...", "s3_key": "...", "tenant_id": "..."}
    """
    activity.logger.info("Calling Textract AnalyzeExpense", extra={"s3_key": inp["s3_key"]})
    client = boto3.client("textract", region_name=settings.aws_region)
    try:
        response = client.analyze_expense(
            Document={"S3Object": {"Bucket": inp["s3_bucket"], "Name": inp["s3_key"]}}
        )
    except client.exceptions.UnsupportedDocumentException as e:
        # ERROR NO-RETRYABLE: el workflow debe marcarlo en RetryPolicy
        raise ApplicationError("UnsupportedMimeTypeError", type="UnsupportedMimeTypeError") from e

    # Guardar el JSON crudo en S3 para linaje (Bronze lo consumirá)
    s3 = boto3.client("s3", region_name=settings.aws_region)
    output_key = f"tenant={inp['tenant_id']}/expense={inp['expense_id']}/{uuid.uuid4()}.json"
    s3.put_object(
        Bucket=settings.s3_textract_output_bucket,
        Key=output_key,
        Body=json.dumps(response).encode("utf-8"),
        ContentType="application/json",
    )

    # Extraer campos normalizados
    summary_fields = _normalize_summary_fields(response)
    return {
        "raw_output_s3_key": output_key,
        "fields": summary_fields,
        "avg_confidence": _compute_avg_confidence(summary_fields),
    }

def _normalize_summary_fields(response: dict) -> dict:
    """De ExpenseDocuments[0].SummaryFields extraer Type, ValueDetection, Confidence."""
    out = {}
    for doc in response.get("ExpenseDocuments", []):
        for field in doc.get("SummaryFields", []):
            key = field.get("Type", {}).get("Text", "OTHER")
            value = field.get("ValueDetection", {}).get("Text")
            confidence = field.get("ValueDetection", {}).get("Confidence", 0.0)
            out[key] = {"value": value, "confidence": confidence}
    return out
```

**Observaciones:**

- **Timeouts.** `start_to_close_timeout=30s` para la sync API. Para documentos grandes usar `start_expense_analysis` async y modelar como dos activities (`start` + `get` con polling via heartbeat).
- **Heartbeating.** Si un activity tarda > 30s, emitir `activity.heartbeat()` cada 5s para que Temporal sepa que sigue vivo.

### 6.2. `publish_event`

```python
# src/nexus_orchestration/activities/redis_events.py
import json
import redis.asyncio as redis
from temporalio import activity
from ulid import ULID
from ..config import settings

_redis_pool: redis.ConnectionPool | None = None

def _get_pool() -> redis.ConnectionPool:
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = redis.ConnectionPool.from_url(settings.redis_url)
    return _redis_pool

@activity.defn(name="publish_event")
async def publish_event(event: dict) -> None:
    """Publica un evento en los canales Redis definidos en el contrato.

    event debe respetar el schema EventEnvelope (schema_version 1.0).
    Esta activity AUTO-COMPLETA event_id, timestamp, schema_version.
    """
    client = redis.Redis(connection_pool=_get_pool())
    envelope = {
        "schema_version": "1.0",
        "event_id": f"evt_{ULID()}",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        **event,
    }
    payload = json.dumps(envelope)

    # Publicar en canal del usuario
    user_channel = f"nexus:events:tenant:{event['tenant_id']}:user:{event['user_id']}"
    await client.publish(user_channel, payload)

    # Publicar en canal del workflow
    if wf_id := event.get("workflow_id") or activity.info().workflow_id:
        wf_channel = f"nexus:events:tenant:{event['tenant_id']}:workflow:{wf_id}"
        await client.publish(wf_channel, payload)

    # Buffer para replay (ZSet)
    score = int(datetime.utcnow().timestamp() * 1000)
    await client.zadd(f"{user_channel}:buffer", {payload: score})
    await client.zremrangebyrank(f"{user_channel}:buffer", 0, -51)
    await client.expire(f"{user_channel}:buffer", 3600)
```

### 6.3. `query_silver_expenses` (activity para el auditor)

```python
@activity.defn(name="query_silver_expense_for_validation")
async def query_silver_expense(inp: dict) -> dict:
    """Consulta silver.expenses vía Databricks SQL para comparar con lo reportado."""
    from databricks import sql
    with sql.connect(
        server_hostname=settings.databricks_host,
        http_path=f"/sql/1.0/warehouses/{settings.databricks_warehouse_id}",
        access_token=settings.databricks_token,
    ) as conn:
        with conn.cursor() as cur:
            # Filtro por tenant_id obligatorio
            cur.execute(
                """
                SELECT expense_id, amount, currency, date, vendor, status
                FROM nexus_prod.silver.expenses
                WHERE expense_id = %(expense_id)s AND tenant_id = %(tenant_id)s
                """,
                {"expense_id": inp["expense_id"], "tenant_id": inp["tenant_id"]},
            )
            row = cur.fetchone()
            return dict(zip([d[0] for d in cur.description], row)) if row else {}
```

### 6.4. Activities Mongo — `upsert_ocr_extraction`, `update_expense_to_approved`, `emit_expense_event`

Estas son las únicas activities con escritura. **El Worker no tiene activities de escritura a Databricks.** Todo lo que el medallion necesita llega vía CDC desde Mongo.

```python
# src/nexus_orchestration/activities/mongodb_writes.py
from datetime import datetime
from temporalio import activity
from ulid import ULID
from motor.motor_asyncio import AsyncIOMotorClient
from ..config import settings

_mongo_client: AsyncIOMotorClient | None = None

def _db():
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = AsyncIOMotorClient(settings.mongodb_uri)
    return _mongo_client[settings.mongodb_db]

@activity.defn(name="upsert_ocr_extraction")
async def upsert_ocr_extraction(inp: dict) -> None:
    """Llamada por OCRExtractionWorkflow al terminar Textract.

    Escribe en la colección ocr_extractions. CDC lo replicará a silver.ocr_extractions
    en ~2 minutos. NO escribe a Databricks directamente.
    """
    await _db().ocr_extractions.update_one(
        {"expense_id": inp["expense_id"]},
        {"$set": {
            "tenant_id": inp["tenant_id"],
            "user_id": inp["user_id"],
            "ocr_total": inp["ocr_total"],
            "ocr_total_confidence": inp["ocr_total_confidence"],
            "ocr_vendor": inp["ocr_vendor"],
            "ocr_vendor_confidence": inp["ocr_vendor_confidence"],
            "ocr_date": inp["ocr_date"],
            "ocr_date_confidence": inp["ocr_date_confidence"],
            "ocr_currency": inp.get("ocr_currency"),
            "avg_confidence": inp["avg_confidence"],
            "textract_raw_s3_key": inp["textract_raw_s3_key"],
            "extracted_by_workflow_id": activity.info().workflow_id,
            "extracted_at": datetime.utcnow(),
        }},
        upsert=True,
    )

@activity.defn(name="update_expense_to_approved")
async def update_expense_to_approved(inp: dict) -> None:
    """Marca el expense como aprobado y embebe los valores finales consolidados.

    Este UPDATE es lo que dispara la promoción a Gold en el medallion.
    Lakeflow detecta status='approved' y materializa gold.expense_audit.
    """
    await _db().expenses.update_one(
        {"expense_id": inp["expense_id"], "tenant_id": inp["tenant_id"]},
        {"$set": {
            "status": "approved",
            "final_amount": inp["final_data"]["amount"],
            "final_vendor": inp["final_data"]["vendor"],
            "final_date": inp["final_data"]["date"],
            "final_currency": inp["final_data"].get("currency"),
            "source_per_field": inp["source_per_field"],
            "approved_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }},
    )

@activity.defn(name="emit_expense_event")
async def emit_expense_event(inp: dict) -> None:
    """Inserta un evento semántico en expense_events.

    Append-only. Es lo que el frontend lee en GET /expenses/{id}/history.
    También llega vía CDC a silver.expense_events para analytics.
    """
    await _db().expense_events.insert_one({
        "event_id": f"evt_{ULID()}",
        "expense_id": inp["expense_id"],
        "tenant_id": inp["tenant_id"],
        "event_type": inp["event_type"],
        "actor": inp["actor"],
        "details": inp.get("details", {}),
        "workflow_id": activity.info().workflow_id,
        "created_at": datetime.utcnow(),
    })
```

**Cuándo se llama `emit_expense_event`** a lo largo de un workflow típico:

| Momento | event_type | Quién emite |
|---|---|---|
| Backend inserta el expense en POST /expenses | `created` | Backend (no Worker) |
| OCRExtractionWorkflow inicia | `ocr_started` | Worker |
| Tras `upsert_ocr_extraction` exitoso | `ocr_completed` | Worker |
| Tras fallo de Textract no recuperable | `ocr_failed` | Worker |
| AuditValidationWorkflow detecta diff | `discrepancy_detected` | Worker |
| AuditValidationWorkflow no detecta diff | `no_discrepancy` | Worker |
| Tras crear hitl_task | `hitl_required` | Worker |
| Tras recibir signal hitl_response | `hitl_resolved` | Worker |
| Tras `update_expense_to_approved` | `approved` | Worker |
| Si workflow falla irrecuperablemente | `failed` | Worker (via on-error) |

Cada uno de estos también dispara un `publish_event` al SSE para reactividad en vivo. El SSE es el canal "ahora", `expense_events` es el canal "después".

---

## 7. Agente Auditor: `AuditValidationWorkflow`

Similar al OCR pero su prompt le dice: "Compara los datos extraídos por OCR con los reportados por el usuario (en silver.expenses), y decide si hay discrepancia. Umbral: diferencia > 1% en `amount`, o texto distinto en `vendor` con similitud < 85%, o fecha ≠. Si hay discrepancia, crea una hitl_task."

Tools disponibles para este agente:
- `query_silver_expense` — lee lo reportado por el usuario.
- `compare_fields` — util pura: dado OCR vs user, devuelve `fields_in_conflict`.
- `create_hitl_task` — inserta en MongoDB `hitl_tasks`.
- `publish_event`.

Retorna `{ "has_discrepancies": bool, "hitl_task_id": str | None, "fields_in_conflict": [...], "extracted_data": {...} }`.

---

## 8. Workflow RAG: `RAGQueryWorkflow`

Agente Exoclaw con tools:
- `vector_search_tool` — consulta `nexus_prod.vector.expense_chunks_index` con filtro SQL `tenant_id = '{tenant_id}'`.
- `publish_event` — emite `chat.token` por token (para UI typewriter). Requiere que el LLM provider soporte streaming y que Exoclaw lo propague.

```python
# Tool para Vector Search
class VectorSearchTool(ToolBase):
    name = "search_expenses"
    description = "Busca en los gastos del usuario por similitud semántica."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "k": {"type": "integer", "default": 5},
        },
        "required": ["query"],
    }

    def __init__(self, tenant_id: str):
        self._tenant_id = tenant_id

    async def execute(self, query: str, k: int = 5) -> str:
        # Llamar activity Temporal para hacer la búsqueda
        result = await workflow.execute_activity(
            "vector_similarity_search",
            {
                "query": query,
                "k": k,
                "tenant_filter": self._tenant_id,  # CRÍTICO: filtrado obligatorio
            },
            start_to_close_timeout=timedelta(seconds=10),
        )
        return json.dumps(result)
```

**⚠️ Crítico:** el `tenant_id` lo inyecta el worker desde el JWT claims (que vino en `inp.tenant_id`). El LLM **nunca** tiene la capacidad de escoger el `tenant_filter` — está hardcoded en el tool.

El activity `vector_similarity_search`:

```python
@activity.defn
async def vector_similarity_search(inp: dict) -> list[dict]:
    from databricks.vector_search.client import VectorSearchClient
    client = VectorSearchClient()
    index = client.get_index(
        endpoint_name=settings.databricks_vs_endpoint,
        index_name=settings.databricks_vs_index,
    )
    results = index.similarity_search(
        query_text=inp["query"],
        columns=["chunk_id", "expense_id", "chunk_text", "date", "vendor", "amount"],
        num_results=inp["k"],
        filters=f'tenant_id = "{inp["tenant_filter"]}"',  # SQL-like filter string
        query_type="HYBRID",  # combina ANN + BM25
    )
    return results.get("result", {}).get("data_array", [])
```

---

## 9. Worker entry point

```python
# src/nexus_orchestration/main_worker.py
import asyncio
from temporalio.client import Client
from temporalio.worker import Worker
from .workflows.expense_audit import ExpenseAuditWorkflow
from .workflows.ocr_extraction import OCRExtractionWorkflow
from .workflows.audit_validation import AuditValidationWorkflow
from .workflows.rag_query import RAGQueryWorkflow
from .activities import textract, redis_events, databricks_sql, vector_search, mongodb

async def run_worker(task_queue: str):
    client = await Client.connect(
        settings.temporal_host,
        namespace=settings.temporal_namespace,
        tls=get_tls_config(),
    )
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[
            ExpenseAuditWorkflow,
            OCRExtractionWorkflow,
            AuditValidationWorkflow,
            RAGQueryWorkflow,
        ],
        activities=[
            textract.textract_analyze_expense,
            redis_events.publish_event,
            databricks_sql.query_silver_expense,
            mongodb_writes.upsert_ocr_extraction,
            mongodb_writes.update_expense_to_approved,
            mongodb_writes.emit_expense_event,
            vector_search.vector_similarity_search,
            mongodb.create_hitl_task,
            mongodb.update_expense_status,
            # ...
        ],
        max_concurrent_activities=20,
        max_concurrent_workflow_tasks=50,
    )
    await worker.run()

if __name__ == "__main__":
    tq = os.environ["TASK_QUEUE"]  # nexus-orchestrator-tq | nexus-ocr-tq | ...
    asyncio.run(run_worker(tq))
```

**Desplegar múltiples workers por task queue** para escalar horizontalmente. Un Kubernetes Deployment por task queue (ver sección de deployment).

---

## 10. Determinismo y reglas de workflow

**Regla absoluta:** dentro de `@workflow.defn`:

- ❌ NO usar `datetime.now()`, `time.time()`, `random`, `uuid.uuid4()`.
- ❌ NO abrir sockets, archivos, o HTTP.
- ❌ NO importar código no determinista en el módulo del workflow.
- ✅ Usar `workflow.now()` para timestamps.
- ✅ Usar `workflow.random()` si se necesita aleatorio.
- ✅ Todo I/O va dentro de `workflow.execute_activity(...)`.

**Sandbox check.** Temporal tiene un sandbox que rechaza imports no deterministas. Configurar `workflow_runner=SandboxedWorkflowRunner()` en `Worker`.

---

## 11. Observabilidad

- **Temporal UI**: expone estado, histórico y signals de cada workflow en un dashboard.
- **Custom SearchAttributes**: al iniciar workflow, setear `TenantId`, `ExpenseId`, `UserId` como search attributes para filtrar en Temporal UI.
- **OpenTelemetry**: pasar `TraceContext` del backend como `Memo` del workflow. Cada activity lo propaga a spans de X-Ray.
- **Métricas Prometheus**: el SDK de Temporal expone métricas. Scraper en CloudWatch Agent.

---

## 12. Pruebas

- **Workflows**: usar `WorkflowEnvironment.from_local_time_skipping()` para tests síncronos con time-skipping — permite testear el timeout HITL de 7 días en milisegundos.
- **Activities**: mocks con `activity.current().info()` para unit tests.
- **Agentes Exoclaw**: usar el `DirectExecutor` (sin Temporal) en tests de lógica agéntica, grabando interacciones con LLM vía VCR-like cassettes.

---

## 13. Deployment (Kubernetes)

Un Deployment por task queue:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nexus-worker-ocr
spec:
  replicas: 3
  template:
    spec:
      containers:
      - name: worker
        image: nexus-orchestration:latest
        env:
        - name: TASK_QUEUE
          value: "nexus-ocr-tq"
        resources:
          requests: { cpu: "500m", memory: "1Gi" }
          limits:   { cpu: "2",    memory: "4Gi" }
```

Workers son **stateless**, scale out con HPA basado en longitud de cola (métrica Temporal `temporal_workflow_task_schedule_to_start_latency`).

---

## 14. Variables de entorno específicas

Hereda todas las del contrato §3. Añade:

```bash
TASK_QUEUE=nexus-orchestrator-tq        # Por worker
WORKER_MAX_CONCURRENT_ACTIVITIES=20
LLM_MODEL=claude-sonnet-4-6
ANTHROPIC_API_KEY=sk-ant-...
DATABRICKS_TOKEN=...
```

---

## 15. Criterios de aceptación

1. **Flujo feliz**: upload → OCR agéntico → auditor determina sin discrepancia → Gold insert. E2E < 30 segundos para un recibo simple.
2. **Flujo HITL**: OCR detecta diferencia en `amount` con confidence 95% vs valor usuario → crea hitl_task → publica evento → usuario responde via signal → workflow resume → Gold insert. Funciona con workflow pausado 24 horas sin pérdida de estado.
3. **Resiliencia**: matar el worker durante un `execute_child_workflow` y reiniciar. El workflow debe reanudar sin repetir trabajo ya completado.
4. **Textract fallback**: si `AnalyzeExpense` devuelve avg_confidence < 80%, el agente invoca `AnalyzeDocument` con Queries y consolida. Test con un recibo borroso.
5. **Multi-tenant**: un workflow con `tenant_id=A` no puede leer datos de Vector Search con `tenant_id=B`, aunque el LLM lo intente (validado con prompt adversarial).
6. **Determinismo**: `WorkflowEnvironment.replay()` sobre un history existente no lanza `NonDeterminismError`.

---

## 16. Referencias

- Exoclaw README (protocolos, ejemplos): https://github.com/Clause-Logic/exoclaw
- Temporal Python SDK: https://python.temporal.io/
- Temporal durable AI cookbook: https://docs.temporal.io/ai-cookbook
- AWS Textract AnalyzeExpense: https://docs.aws.amazon.com/textract/latest/dg/expensedocuments.html
- Databricks Vector Search Python SDK: https://api-docs.databricks.com/python/vector-search/
