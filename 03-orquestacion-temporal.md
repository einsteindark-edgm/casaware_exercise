# 03 · Orquestación — Temporal Workers

> **Rol en Nexus.** Este sistema ejecuta los **workflows duraderos** que orquestan la auditoría de cada gasto y las consultas del chatbot RAG. Es el único sistema que llama a Textract, Databricks SQL y Vector Search; el backend nunca lo hace directamente.
>
> **Pre-requisito de lectura:** [`00-contratos-compartidos.md`](./00-contratos-compartidos.md). Los contratos de workflows, signals, canales Redis y schemas vienen de ahí.

---

## 1. Decisión arquitectónica: Temporal sin framework de agentes

Este proyecto usa **solo Temporal.io + el SDK del LLM (AWS Bedrock o Anthropic API)**. No usa frameworks de agentes (LangGraph, Exoclaw, CrewAI, etc.).

### Por qué solo Temporal

Temporal aporta lo difícil: durabilidad, retries, signals, queries, checkpointing automático. Un workflow que se pausa 7 días esperando una decisión humana resume exactamente donde quedó, sobreviviendo a despliegues y caídas de workers.

Para los casos de uso de Nexus, el "agente" no es siempre un loop ReAct sofisticado:

- **OCR extraction**: no necesita LLM. Textract devuelve datos estructurados con confidence scores; la lógica "si confidence < 80%, retry con AnalyzeDocument Queries" es un `if` en código Python, no una decisión de LLM.
- **Audit validation**: no necesita LLM. Comparar dos números con tolerancia, dos strings con similitud, dos fechas con igualdad — código determinista. Mejor para testabilidad y costo.
- **RAG chatbot**: SÍ necesita un loop agéntico mínimo (LLM decide qué chunks buscar, formula la respuesta). Implementado a mano son ~80 líneas, sin dependencias extras.

### Por qué NO un framework de agentes

Un framework como Exoclaw te ahorraría las ~50 líneas del agent loop pero te cuesta:

- Aprender los protocolos del framework.
- Aprender específicamente cómo se integra con Temporal.
- Instalar y resolver compatibilidades de varios paquetes nuevos.
- Debuggear con más capas en el stack trace.

Para un proyecto de scope acotado, escribir el loop a mano es más rápido y deja código más legible. Si en el futuro el sistema crece y necesitas patrones agénticos sofisticados (multi-agent, planning, reflection), entonces evalúa migrar.

**Referencias clave:**
- Temporal Python SDK: https://python.temporal.io/
- Temporal AI cookbook: https://docs.temporal.io/ai-cookbook
- AWS Bedrock Converse API: https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html
- Anthropic tool use: https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/overview

---

## 2. Stack técnico

| Componente | Librería / Versión | Notas |
|---|---|---|
| Workflow engine | **temporalio ≥ 1.8** | SDK Python oficial |
| LLM provider | **boto3 1.34+** (Bedrock Converse API) o **anthropic 0.34+** | Bedrock recomendado para integración nativa con AWS |
| AWS Textract | **boto3 1.34+** | `analyze_expense` síncrono, `start_expense_analysis` async |
| Databricks SQL | **databricks-sql-connector 3.3+** | Lectura de Silver para auditor |
| Databricks Vector Search | **databricks-vectorsearch 0.50+** | Cliente oficial |
| MongoDB | **motor 3.5+** (async) | Writes desde activities |
| Comparación strings | **rapidfuzz 3.9+** | Similitud Levenshtein para vendor matching |
| Redis | **redis-py 5.0+** (async) | Publish a canales del contrato |
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
│       │   ├── ocr_extraction.py        # OCRExtractionWorkflow (child, sin LLM)
│       │   ├── audit_validation.py      # AuditValidationWorkflow (child, sin LLM)
│       │   └── rag_query.py             # RAGQueryWorkflow (loop agéntico mínimo)
│       │
│       ├── activities/
│       │   ├── __init__.py
│       │   ├── textract.py              # textract_analyze_expense, textract_queries
│       │   ├── databricks_sql.py        # query_silver_expense (read-only)
│       │   ├── vector_search.py         # similarity_search
│       │   ├── mongodb_writes.py        # upsert_ocr_extraction, update_expense_to_approved, emit_expense_event, create_hitl_task
│       │   ├── redis_events.py          # publish_event (respeta schema del contrato)
│       │   ├── llm.py                   # bedrock_converse / anthropic_messages
│       │   └── comparison.py            # compare_fields para el auditor
│       │
│       ├── tools/                       # Definiciones de tools para el LLM (RAG)
│       │   ├── __init__.py
│       │   └── search_expenses.py       # Schema + dispatcher
│       │
│       ├── prompts/
│       │   └── rag_system.md            # System prompt del chatbot
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

        # 2) Child: OCR (workflow simple, sin LLM)
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

        # 3) Child: Auditoría (compara OCR vs Mongo, sin LLM)
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

            final_data = self._apply_hitl_resolution(audit_result, self._hitl_response)
            source_per_field = self._compute_source_per_field(audit_result, self._hitl_response)
        else:
            final_data = audit_result["extracted_data"]
            source_per_field = {f: "ocr_high_confidence" for f in final_data}

        # 5) Persistir resolución en Mongo. NO escribir a Databricks.
        # Lakeflow detectará el cambio de status vía CDC y promoverá a Gold.
        self._current_step = "finalizing_in_mongo"
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

        # 6) Emitir evento semántico para el timeline
        await workflow.execute_activity(
            "emit_expense_event",
            {
                "expense_id": inp.expense_id,
                "tenant_id": inp.tenant_id,
                "event_type": "approved",
                "actor": {"type": "system", "id": "worker:nexus-orchestrator-tq"},
                "details": {"final_data": final_data, "source_per_field": source_per_field},
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

## 5. Child workflow: `OCRExtractionWorkflow` (sin LLM)

Este workflow no usa LLM. La "decisión" del retry con queries específicas es un `if` simple basado en el confidence promedio.

```python
# src/nexus_orchestration/workflows/ocr_extraction.py
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy
from ..schemas.inputs import ExpenseAuditInput

@workflow.defn(name="OCRExtractionWorkflow")
class OCRExtractionWorkflow:
    @workflow.run
    async def run(self, inp: ExpenseAuditInput) -> dict:
        # 1) Notificar inicio
        await workflow.execute_activity(
            "emit_expense_event",
            {
                "expense_id": inp.expense_id,
                "tenant_id": inp.tenant_id,
                "event_type": "ocr_started",
                "actor": {"type": "system", "id": "worker:ocr"},
                "details": {"s3_key": inp.receipt_s3_key},
            },
            start_to_close_timeout=timedelta(seconds=5),
        )

        # 2) Llamar Textract AnalyzeExpense
        result = await workflow.execute_activity(
            "textract_analyze_expense",
            {
                "s3_bucket": inp.receipt_s3_bucket,
                "s3_key": inp.receipt_s3_key,
                "tenant_id": inp.tenant_id,
                "expense_id": inp.expense_id,
            },
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                maximum_attempts=3,
                non_retryable_error_types=["UnsupportedMimeTypeError"],
            ),
        )

        # 3) Si confidence promedio bajo, intentar con AnalyzeDocument + Queries
        if result["avg_confidence"] < 80:
            queries_result = await workflow.execute_activity(
                "textract_analyze_document_queries",
                {
                    "s3_bucket": inp.receipt_s3_bucket,
                    "s3_key": inp.receipt_s3_key,
                    "queries": [
                        {"Text": "What is the total amount?", "Alias": "TOTAL"},
                        {"Text": "What is the date?", "Alias": "DATE"},
                        {"Text": "What is the vendor name?", "Alias": "VENDOR"},
                    ],
                },
                start_to_close_timeout=timedelta(seconds=30),
            )
            # Consolidar: preferir el que tenga mayor confidence por campo
            result = _merge_extractions(result, queries_result)

        # 4) Persistir en Mongo. NO escribir a Databricks.
        await workflow.execute_activity(
            "upsert_ocr_extraction",
            {
                "expense_id": inp.expense_id,
                "tenant_id": inp.tenant_id,
                "user_id": inp.user_id,
                **result["fields"],
                "avg_confidence": result["avg_confidence"],
                "textract_raw_s3_key": result["raw_output_s3_key"],
            },
            start_to_close_timeout=timedelta(seconds=10),
        )

        # 5) Notificar finalización
        await workflow.execute_activity(
            "emit_expense_event",
            {
                "expense_id": inp.expense_id,
                "tenant_id": inp.tenant_id,
                "event_type": "ocr_completed",
                "actor": {"type": "system", "id": "worker:ocr"},
                "details": {
                    "avg_confidence": result["avg_confidence"],
                    "fields_summary": result["fields_summary"],
                },
            },
            start_to_close_timeout=timedelta(seconds=5),
        )

        return result
```

**Por qué no necesita LLM**: Textract devuelve datos ya estructurados (`SummaryFields` con `Type` y `ValueDetection`). La decisión de fallback es una regla simple basada en confidence. Un LLM aquí solo añadiría costo, latencia y no-determinismo.

---

## 6. Child workflow: `AuditValidationWorkflow` (sin LLM)

```python
# src/nexus_orchestration/workflows/audit_validation.py
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy

@workflow.defn(name="AuditValidationWorkflow")
class AuditValidationWorkflow:
    @workflow.run
    async def run(self, inp: dict) -> dict:
        expense_input = inp["expense_input"]
        ocr_result = inp["ocr_result"]

        # 1) Leer datos reportados por el usuario desde Silver (poblado por CDC)
        user_reported = await workflow.execute_activity(
            "query_silver_expense_for_validation",
            {
                "expense_id": expense_input.expense_id,
                "tenant_id": expense_input.tenant_id,
            },
            start_to_close_timeout=timedelta(seconds=15),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

        # 2) Comparar campos (función pura, sin LLM)
        comparison = await workflow.execute_activity(
            "compare_fields",
            {
                "user_reported": user_reported,
                "ocr_extracted": ocr_result["fields"],
                "tolerance": {"amount_pct": 0.01, "vendor_similarity_min": 0.85},
            },
            start_to_close_timeout=timedelta(seconds=5),
        )

        # 3) Si hay discrepancias, crear hitl_task
        hitl_task_id = None
        if comparison["fields_in_conflict"]:
            hitl_task_id = await workflow.execute_activity(
                "create_hitl_task",
                {
                    "workflow_id": workflow.info().parent.workflow_id,
                    "tenant_id": expense_input.tenant_id,
                    "user_id": expense_input.user_id,
                    "expense_id": expense_input.expense_id,
                    "fields_in_conflict": comparison["fields_in_conflict"],
                },
                start_to_close_timeout=timedelta(seconds=10),
            )
            await workflow.execute_activity(
                "emit_expense_event",
                {
                    "expense_id": expense_input.expense_id,
                    "tenant_id": expense_input.tenant_id,
                    "event_type": "discrepancy_detected",
                    "actor": {"type": "system", "id": "worker:auditor"},
                    "details": {
                        "fields_in_conflict": comparison["fields_in_conflict"],
                        "hitl_task_id": hitl_task_id,
                    },
                },
                start_to_close_timeout=timedelta(seconds=5),
            )
        else:
            await workflow.execute_activity(
                "emit_expense_event",
                {
                    "expense_id": expense_input.expense_id,
                    "tenant_id": expense_input.tenant_id,
                    "event_type": "no_discrepancy",
                    "actor": {"type": "system", "id": "worker:auditor"},
                    "details": {},
                },
                start_to_close_timeout=timedelta(seconds=5),
            )

        return {
            "has_discrepancies": bool(comparison["fields_in_conflict"]),
            "fields_in_conflict": comparison["fields_in_conflict"],
            "hitl_task_id": hitl_task_id,
            "extracted_data": ocr_result["fields"],
        }
```

### 6.1. Activity `compare_fields` — código puro

```python
# src/nexus_orchestration/activities/comparison.py
from rapidfuzz import fuzz
from temporalio import activity

@activity.defn(name="compare_fields")
async def compare_fields(inp: dict) -> dict:
    user = inp["user_reported"]
    ocr = inp["ocr_extracted"]
    tol = inp["tolerance"]

    conflicts = []

    # amount: diferencia porcentual
    user_amount = float(user.get("amount") or 0)
    ocr_amount = float(ocr.get("ocr_total", {}).get("value") or 0)
    if user_amount and ocr_amount:
        diff_pct = abs(user_amount - ocr_amount) / max(user_amount, ocr_amount)
        if diff_pct > tol["amount_pct"]:
            conflicts.append({
                "field": "amount",
                "user_value": user_amount,
                "ocr_value": ocr_amount,
                "confidence": ocr.get("ocr_total", {}).get("confidence"),
            })

    # vendor: similitud Levenshtein
    user_vendor = (user.get("vendor") or "").strip().lower()
    ocr_vendor = (ocr.get("ocr_vendor", {}).get("value") or "").strip().lower()
    if user_vendor and ocr_vendor:
        similarity = fuzz.ratio(user_vendor, ocr_vendor) / 100
        if similarity < tol["vendor_similarity_min"]:
            conflicts.append({
                "field": "vendor",
                "user_value": user_vendor,
                "ocr_value": ocr_vendor,
                "confidence": ocr.get("ocr_vendor", {}).get("confidence"),
                "similarity": similarity,
            })

    # date: igualdad exacta
    if user.get("date") != ocr.get("ocr_date", {}).get("value"):
        conflicts.append({
            "field": "date",
            "user_value": user.get("date"),
            "ocr_value": ocr.get("ocr_date", {}).get("value"),
            "confidence": ocr.get("ocr_date", {}).get("confidence"),
        })

    return {"fields_in_conflict": conflicts}
```

**Ventajas de no usar LLM aquí**: testeable con casos unitarios deterministas, costo cero por ejecución, latencia ~ms en lugar de segundos, sin riesgo de alucinación.

---

## 7. RAG Workflow: `RAGQueryWorkflow` (loop agéntico mínimo, sin framework)

Este es el único workflow que usa LLM. Implementado a mano con AWS Bedrock Converse API. ~80 líneas de workflow + ~80 de activity Bedrock.

### 7.1. El workflow

```python
# src/nexus_orchestration/workflows/rag_query.py
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy

# Schema del único tool disponible para el LLM
SEARCH_EXPENSES_TOOL = {
    "name": "search_expenses",
    "description": (
        "Busca en los gastos del usuario por similitud semántica. "
        "Usa esta herramienta para responder preguntas sobre gastos pasados."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Consulta en lenguaje natural"},
            "k": {"type": "integer", "description": "Número de resultados", "default": 5},
        },
        "required": ["query"],
    },
}

@workflow.defn(name="RAGQueryWorkflow")
class RAGQueryWorkflow:
    def __init__(self) -> None:
        self._cancelled = False

    @workflow.run
    async def run(self, inp: dict) -> dict:
        """
        inp = {
            "tenant_id": "...", "user_id": "...",
            "session_id": "...", "turn_n": 1,
            "user_message": "¿Cuánto gasté en café en febrero?",
            "previous_turns": [{"role": "user|assistant", "content": "..."}, ...]
        }
        """
        system_prompt = await workflow.execute_activity(
            "load_rag_system_prompt",
            start_to_close_timeout=timedelta(seconds=2),
        )

        messages = list(inp["previous_turns"]) + [
            {"role": "user", "content": [{"text": inp["user_message"]}]}
        ]

        max_iterations = 5
        for iteration in range(max_iterations):
            if self._cancelled:
                return {"status": "cancelled"}

            response = await workflow.execute_activity(
                "bedrock_converse",
                {
                    "system": system_prompt,
                    "messages": messages,
                    "tools": [SEARCH_EXPENSES_TOOL],
                    "stream_to_redis": True,  # publica chat.token por cada token
                    "tenant_id": inp["tenant_id"],
                    "user_id": inp["user_id"],
                    "workflow_id": workflow.info().workflow_id,
                },
                start_to_close_timeout=timedelta(seconds=60),
                heartbeat_timeout=timedelta(seconds=10),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )

            # Bedrock devuelve el mensaje completo + stop_reason
            messages.append({"role": "assistant", "content": response["content"]})

            if response["stop_reason"] != "tool_use":
                # El LLM terminó. Publicar chat.complete con citations.
                citations = self._extract_citations_from_history(messages)
                final_text = self._extract_final_text(response["content"])
                await workflow.execute_activity(
                    "publish_event",
                    {
                        "event_type": "chat.complete",
                        "tenant_id": inp["tenant_id"],
                        "user_id": inp["user_id"],
                        "payload": {
                            "session_id": inp["session_id"],
                            "citations": citations,
                            "final_text": final_text,
                        },
                    },
                    start_to_close_timeout=timedelta(seconds=5),
                )
                # Persistir el turn en Mongo para historial de conversación
                await workflow.execute_activity(
                    "save_chat_turn",
                    {
                        "session_id": inp["session_id"],
                        "tenant_id": inp["tenant_id"],
                        "user_id": inp["user_id"],
                        "turn_n": inp["turn_n"],
                        "user_message": inp["user_message"],
                        "assistant_message": final_text,
                        "citations": citations,
                    },
                    start_to_close_timeout=timedelta(seconds=10),
                )
                return {"status": "completed", "text": final_text, "citations": citations}

            # El LLM pidió usar tools. Ejecutarlos y agregar resultados.
            tool_results = []
            for block in response["content"]:
                if block.get("type") != "tool_use":
                    continue
                if block["name"] == "search_expenses":
                    # CRÍTICO: el tenant_id viene del input del workflow,
                    # NO de los argumentos del LLM. Aislamiento multi-tenant.
                    search_result = await workflow.execute_activity(
                        "vector_similarity_search",
                        {
                            "query": block["input"]["query"],
                            "k": block["input"].get("k", 5),
                            "tenant_filter": inp["tenant_id"],
                        },
                        start_to_close_timeout=timedelta(seconds=10),
                    )
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": [{"json": search_result}],
                    })
                else:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": [{"text": f"Tool {block['name']} no disponible"}],
                        "status": "error",
                    })

            messages.append({"role": "user", "content": tool_results})

        # Si llegamos aquí, el LLM excedió max_iterations
        await workflow.execute_activity(
            "publish_event",
            {
                "event_type": "chat.complete",
                "tenant_id": inp["tenant_id"],
                "user_id": inp["user_id"],
                "payload": {
                    "session_id": inp["session_id"],
                    "error": "max_iterations_exceeded",
                },
            },
            start_to_close_timeout=timedelta(seconds=5),
        )
        return {"status": "max_iterations_exceeded"}

    @workflow.signal(name="cancel_chat")
    async def cancel_chat(self, payload: dict) -> None:
        self._cancelled = True

    def _extract_citations_from_history(self, messages: list) -> list:
        """Recolecta los chunks devueltos por search_expenses durante el loop."""
        citations = []
        for msg in messages:
            if msg["role"] != "user":
                continue
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    for c in block.get("content", []):
                        if "json" in c and isinstance(c["json"], list):
                            citations.extend(c["json"])
        return citations[:10]  # Limitar para no saturar el frontend

    def _extract_final_text(self, content: list) -> str:
        for block in content:
            if isinstance(block, dict) and block.get("text"):
                return block["text"]
        return ""
```

### 7.2. Activity `bedrock_converse` con streaming

```python
# src/nexus_orchestration/activities/llm.py
import boto3
import json
from datetime import datetime
from temporalio import activity
from ulid import ULID
from ..config import settings

@activity.defn(name="bedrock_converse")
async def bedrock_converse(inp: dict) -> dict:
    """
    Llama Bedrock Converse API. Si stream_to_redis=True, publica un evento
    chat.token por cada token recibido. Devuelve el mensaje completo al final.
    """
    client = boto3.client("bedrock-runtime", region_name=settings.aws_region)

    request = {
        "modelId": settings.bedrock_model_id,  # ej: "anthropic.claude-sonnet-4-6-20250514-v1:0"
        "messages": inp["messages"],
        "system": [{"text": inp["system"]}],
        "toolConfig": {
            "tools": [{"toolSpec": {
                "name": t["name"],
                "description": t["description"],
                "inputSchema": {"json": t["input_schema"]},
            }} for t in inp["tools"]]
        },
        "inferenceConfig": {"maxTokens": 4096, "temperature": 0.2},
    }

    if not inp.get("stream_to_redis"):
        # Modo síncrono simple
        response = client.converse(**request)
        return {
            "content": response["output"]["message"]["content"],
            "stop_reason": response["stopReason"],
        }

    # Modo streaming: publicar token a token a Redis
    response = client.converse_stream(**request)
    accumulated_content = []
    current_text = ""
    current_tool_use = None
    stop_reason = "end_turn"

    redis_pub = _get_redis()

    for event in response["stream"]:
        # Heartbeat periódico para Temporal
        activity.heartbeat()

        if "contentBlockDelta" in event:
            delta = event["contentBlockDelta"]["delta"]
            if "text" in delta:
                token = delta["text"]
                current_text += token
                # Publicar token a Redis para SSE
                await redis_pub.publish(
                    f"nexus:events:tenant:{inp['tenant_id']}:workflow:{inp['workflow_id']}",
                    json.dumps({
                        "schema_version": "1.0",
                        "event_id": f"evt_{ULID()}",
                        "event_type": "chat.token",
                        "tenant_id": inp["tenant_id"],
                        "user_id": inp["user_id"],
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                        "payload": {"token": token},
                    }),
                )
            elif "toolUse" in delta:
                if current_tool_use:
                    current_tool_use["input_json"] += delta["toolUse"].get("input", "")
        elif "contentBlockStart" in event:
            start = event["contentBlockStart"]["start"]
            if "toolUse" in start:
                current_tool_use = {
                    "type": "tool_use",
                    "id": start["toolUse"]["toolUseId"],
                    "name": start["toolUse"]["name"],
                    "input_json": "",
                }
        elif "contentBlockStop" in event:
            if current_tool_use:
                current_tool_use["input"] = json.loads(current_tool_use.pop("input_json"))
                accumulated_content.append(current_tool_use)
                current_tool_use = None
            elif current_text:
                accumulated_content.append({"type": "text", "text": current_text})
                current_text = ""
        elif "messageStop" in event:
            stop_reason = event["messageStop"]["stopReason"]

    return {"content": accumulated_content, "stop_reason": stop_reason}
```

### 7.3. Activity `vector_similarity_search`

```python
# src/nexus_orchestration/activities/vector_search.py
from databricks.vector_search.client import VectorSearchClient
from temporalio import activity
from ..config import settings

@activity.defn(name="vector_similarity_search")
async def vector_similarity_search(inp: dict) -> list[dict]:
    """
    CRÍTICO: tenant_filter viene del workflow, NO de los argumentos del LLM.
    Vector Search no soporta RLS de Unity Catalog. El aislamiento depende de
    que este filtro se aplique en cada query.
    """
    client = VectorSearchClient()
    index = client.get_index(
        endpoint_name=settings.databricks_vs_endpoint,
        index_name=settings.databricks_vs_index,
    )
    results = index.similarity_search(
        query_text=inp["query"],
        columns=["chunk_id", "expense_id", "chunk_text", "date", "vendor", "amount"],
        num_results=inp["k"],
        filters=f'tenant_id = "{inp["tenant_filter"]}"',
        query_type="HYBRID",
    )
    return results.get("result", {}).get("data_array", [])
```

### 7.4. System prompt (`prompts/rag_system.md`)

```markdown
Eres el asistente financiero de Nexus. Respondes preguntas sobre los gastos
del usuario consultando su historial.

Reglas:
- SIEMPRE usa la herramienta `search_expenses` antes de responder. No inventes números.
- Si la búsqueda devuelve resultados vacíos, di "No encontré gastos que coincidan"
  en lugar de inventar.
- Las cantidades, fechas y nombres de vendor deben venir literalmente de los
  resultados de búsqueda. No los modifiques.
- Sé conciso. Una respuesta de 1-3 oraciones suele ser suficiente.
- Si la pregunta es ambigua (ej: "¿gasté mucho?"), pide aclaración antes de buscar.
```

**Sobre por qué este loop manual basta:** ~150 líneas totales (workflow + activity Bedrock + tool dispatcher). Un framework de agentes te ahorraría quizás 50 de esas líneas a costa de añadir dependencias, conceptos nuevos y debugging extra. Para un solo agente con un solo tool, no vale la pena.

---

## 8. Activities — implementación de referencia

### 8.1. `textract_analyze_expense`

```python
# src/nexus_orchestration/activities/textract.py
import boto3
import json
import uuid
from temporalio import activity
from temporalio.exceptions import ApplicationError
from ..config import settings

@activity.defn(name="textract_analyze_expense")
async def textract_analyze_expense(inp: dict) -> dict:
    activity.logger.info("Calling Textract AnalyzeExpense", extra={"s3_key": inp["s3_key"]})
    client = boto3.client("textract", region_name=settings.aws_region)
    try:
        response = client.analyze_expense(
            Document={"S3Object": {"Bucket": inp["s3_bucket"], "Name": inp["s3_key"]}}
        )
    except client.exceptions.UnsupportedDocumentException as e:
        raise ApplicationError("UnsupportedMimeType", type="UnsupportedMimeTypeError") from e

    # Guardar JSON crudo en S3 para linaje (no como fuente principal)
    s3 = boto3.client("s3", region_name=settings.aws_region)
    output_key = f"tenant={inp['tenant_id']}/expense={inp['expense_id']}/{uuid.uuid4()}.json"
    s3.put_object(
        Bucket=settings.s3_textract_output_bucket,
        Key=output_key,
        Body=json.dumps(response).encode("utf-8"),
        ContentType="application/json",
    )

    summary_fields = _normalize_summary_fields(response)
    return {
        "raw_output_s3_key": output_key,
        "fields": summary_fields,
        "avg_confidence": _compute_avg_confidence(summary_fields),
        "fields_summary": _build_summary(summary_fields),
    }

def _normalize_summary_fields(response: dict) -> dict:
    """De ExpenseDocuments[0].SummaryFields extraer Type, ValueDetection, Confidence."""
    out = {}
    for doc in response.get("ExpenseDocuments", []):
        for field in doc.get("SummaryFields", []):
            key = field.get("Type", {}).get("Text", "OTHER")
            value = field.get("ValueDetection", {}).get("Text")
            confidence = field.get("ValueDetection", {}).get("Confidence", 0.0)
            normalized_key = {
                "TOTAL": "ocr_total",
                "VENDOR_NAME": "ocr_vendor",
                "INVOICE_RECEIPT_DATE": "ocr_date",
            }.get(key)
            if normalized_key:
                out[normalized_key] = {"value": value, "confidence": confidence}
    return out
```

### 8.2. `publish_event`

```python
# src/nexus_orchestration/activities/redis_events.py
import json
import redis.asyncio as redis
from datetime import datetime
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
    client = redis.Redis(connection_pool=_get_pool())
    envelope = {
        "schema_version": "1.0",
        "event_id": f"evt_{ULID()}",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        **event,
    }
    payload = json.dumps(envelope)

    user_channel = f"nexus:events:tenant:{event['tenant_id']}:user:{event['user_id']}"
    await client.publish(user_channel, payload)

    if wf_id := event.get("workflow_id") or activity.info().workflow_id:
        wf_channel = f"nexus:events:tenant:{event['tenant_id']}:workflow:{wf_id}"
        await client.publish(wf_channel, payload)

    score = int(datetime.utcnow().timestamp() * 1000)
    await client.zadd(f"{user_channel}:buffer", {payload: score})
    await client.zremrangebyrank(f"{user_channel}:buffer", 0, -51)
    await client.expire(f"{user_channel}:buffer", 3600)
```

### 8.3. `query_silver_expenses` (auditor)

```python
@activity.defn(name="query_silver_expense_for_validation")
async def query_silver_expense(inp: dict) -> dict:
    from databricks import sql
    with sql.connect(
        server_hostname=settings.databricks_host,
        http_path=f"/sql/1.0/warehouses/{settings.databricks_warehouse_id}",
        access_token=settings.databricks_token,
    ) as conn:
        with conn.cursor() as cur:
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

### 8.4. Activities Mongo

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
    """Persiste el resultado del OCR. CDC lo replica a silver.ocr_extractions."""
    await _db().ocr_extractions.update_one(
        {"expense_id": inp["expense_id"]},
        {"$set": {
            "tenant_id": inp["tenant_id"],
            "user_id": inp["user_id"],
            "ocr_total": inp.get("ocr_total", {}).get("value"),
            "ocr_total_confidence": inp.get("ocr_total", {}).get("confidence"),
            "ocr_vendor": inp.get("ocr_vendor", {}).get("value"),
            "ocr_vendor_confidence": inp.get("ocr_vendor", {}).get("confidence"),
            "ocr_date": inp.get("ocr_date", {}).get("value"),
            "ocr_date_confidence": inp.get("ocr_date", {}).get("confidence"),
            "avg_confidence": inp["avg_confidence"],
            "textract_raw_s3_key": inp["textract_raw_s3_key"],
            "extracted_by_workflow_id": activity.info().workflow_id,
            "extracted_at": datetime.utcnow(),
        }},
        upsert=True,
    )

@activity.defn(name="update_expense_to_approved")
async def update_expense_to_approved(inp: dict) -> None:
    """Marca el expense como approved. Lakeflow detecta el cambio y promueve a Gold."""
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
    """Append-only en expense_events. Fuente del endpoint GET /expenses/{id}/history."""
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

@activity.defn(name="create_hitl_task")
async def create_hitl_task(inp: dict) -> str:
    task_id = f"hitl_{ULID()}"
    await _db().hitl_tasks.insert_one({
        "task_id": task_id,
        "workflow_id": inp["workflow_id"],
        "tenant_id": inp["tenant_id"],
        "user_id": inp["user_id"],
        "expense_id": inp["expense_id"],
        "fields_in_conflict": inp["fields_in_conflict"],
        "status": "pending",
        "created_at": datetime.utcnow(),
    })
    return task_id
```

**Tabla de cuándo emitir cada `event_type`:**

| Momento | event_type | Quién emite |
|---|---|---|
| Backend inserta el expense | `created` | Backend (no Worker) |
| OCRExtractionWorkflow inicia | `ocr_started` | Worker |
| Tras `upsert_ocr_extraction` exitoso | `ocr_completed` | Worker |
| Fallo de Textract no recuperable | `ocr_failed` | Worker |
| AuditValidationWorkflow detecta diff | `discrepancy_detected` | Worker |
| AuditValidationWorkflow no detecta diff | `no_discrepancy` | Worker |
| Tras crear hitl_task | `hitl_required` | Worker |
| Tras recibir signal hitl_response | `hitl_resolved` | Worker |
| Tras `update_expense_to_approved` | `approved` | Worker |
| Workflow falla irrecuperablemente | `failed` | Worker (vía on-error) |

---

## 9. Worker entry point

```python
# src/nexus_orchestration/main_worker.py
import asyncio
import os
from temporalio.client import Client
from temporalio.worker import Worker
from .workflows.expense_audit import ExpenseAuditWorkflow
from .workflows.ocr_extraction import OCRExtractionWorkflow
from .workflows.audit_validation import AuditValidationWorkflow
from .workflows.rag_query import RAGQueryWorkflow
from .activities import textract, redis_events, databricks_sql, vector_search, mongodb_writes, llm, comparison
from .config import settings, get_tls_config

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
            textract.textract_analyze_document_queries,
            redis_events.publish_event,
            databricks_sql.query_silver_expense,
            mongodb_writes.upsert_ocr_extraction,
            mongodb_writes.update_expense_to_approved,
            mongodb_writes.emit_expense_event,
            mongodb_writes.create_hitl_task,
            vector_search.vector_similarity_search,
            llm.bedrock_converse,
            llm.load_rag_system_prompt,
            llm.save_chat_turn,
            comparison.compare_fields,
        ],
        max_concurrent_activities=20,
        max_concurrent_workflow_tasks=50,
    )
    await worker.run()

if __name__ == "__main__":
    tq = os.environ["TASK_QUEUE"]
    asyncio.run(run_worker(tq))
```

**Desplegar múltiples workers por task queue** para escalar horizontalmente. Un Kubernetes Deployment por task queue.

---

## 10. Determinismo y reglas de workflow

**Regla absoluta:** dentro de `@workflow.defn`:

- ❌ NO usar `datetime.now()`, `time.time()`, `random`, `uuid.uuid4()`.
- ❌ NO abrir sockets, archivos, o HTTP.
- ❌ NO importar código no determinista en el módulo del workflow.
- ✅ Usar `workflow.now()` para timestamps.
- ✅ Usar `workflow.random()` si se necesita aleatorio.
- ✅ Todo I/O va dentro de `workflow.execute_activity(...)`.

**Sandbox check.** Configurar `workflow_runner=SandboxedWorkflowRunner()` en `Worker`.

---

## 11. Observabilidad

- **Temporal UI**: dashboard de estado, histórico y signals de cada workflow.
- **SearchAttributes**: setear `TenantId`, `ExpenseId`, `UserId` al iniciar workflows para filtrar en Temporal UI.
- **OpenTelemetry**: pasar `TraceContext` del backend como `Memo` del workflow.
- **Métricas Prometheus**: el SDK de Temporal expone métricas. Scraper en CloudWatch Agent.

---

## 12. Pruebas

- **Workflows**: usar `WorkflowEnvironment.from_local_time_skipping()` para tests con time-skipping (timeout HITL de 7 días en milisegundos).
- **Activities**: mocks normales con pytest.
- **`compare_fields`**: tests unitarios cubriendo: amounts dentro/fuera de tolerancia, vendor con typos, fechas distintas. Sin mocks porque es función pura.
- **RAG workflow**: tests con respuestas de Bedrock grabadas (cassettes de pytest-vcr).

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

Workers son **stateless**, scale out con HPA basado en longitud de cola.

---

## 14. Variables de entorno específicas

Hereda todas las del contrato §3. Añade:

```bash
TASK_QUEUE=nexus-orchestrator-tq
WORKER_MAX_CONCURRENT_ACTIVITIES=20
BEDROCK_MODEL_ID=anthropic.claude-sonnet-4-6-20250514-v1:0
DATABRICKS_TOKEN=...
```

---

## 15. Criterios de aceptación

1. **Flujo feliz**: upload → OCR → auditor sin discrepancia → update Mongo → Lakeflow promueve a Gold. E2E < 30s desde upload hasta status=approved en Mongo.
2. **Flujo HITL**: discrepancia → hitl_task → signal hitl_response → update Mongo. Funciona con workflow pausado 24h sin pérdida de estado.
3. **Resiliencia**: matar el worker durante un `execute_child_workflow` y reiniciar. El workflow debe reanudar sin repetir trabajo ya completado.
4. **Textract fallback**: si `AnalyzeExpense` devuelve avg_confidence < 80%, el workflow llama `AnalyzeDocument` con Queries y consolida. Test con un recibo borroso.
5. **Multi-tenant**: un workflow con `tenant_id=A` no puede leer datos de Vector Search con `tenant_id=B`. El `tenant_filter` en `vector_similarity_search` viene del input del workflow, NO de los argumentos del LLM.
6. **Determinismo**: `WorkflowEnvironment.replay()` sobre un history existente no lanza `NonDeterminismError`.
7. **RAG streaming**: el frontend ve tokens aparecer a ≥30 tokens/seg percibidos durante una respuesta del chatbot.

---

## 16. Referencias

- Temporal Python SDK: https://python.temporal.io/
- Temporal AI cookbook: https://docs.temporal.io/ai-cookbook
- AWS Bedrock Converse API: https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html
- AWS Textract AnalyzeExpense: https://docs.aws.amazon.com/textract/latest/dg/expensedocuments.html
- Databricks Vector Search Python SDK: https://api-docs.databricks.com/python/vector-search/
