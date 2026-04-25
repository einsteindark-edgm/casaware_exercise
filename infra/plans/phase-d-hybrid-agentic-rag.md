# Phase D — Hybrid Agentic RAG (Chat SQL + Semántico)

**Goal**: evolucionar el chat actual (vector-only) a un **hybrid agentic RAG** donde el LLM (Claude en Bedrock, orquestado por Temporal) decide por sí mismo, turno a turno, si responder con:

1. **SQL estructurado** sobre `gold.expense_audit` — para preguntas con filtros exactos ("¿cuánto gasté en Uber en marzo?", "dame mis recibos de $24,395").
2. **Búsqueda semántica** sobre `gold.expense_chunks` indexado en Databricks Vector Search — para preguntas difusas ("¿qué compré de comida la semana pasada?", "gastos relacionados con transporte al aeropuerto").
3. **Combinación de ambas** en un mismo turno (agentic loop — ya soportado, solo hay que darle las dos tools).

Cada respuesta incluye **evidencia citable**: un listado de `expense_id`s con link deep-link al frontend (`/expenses/exp_01KQ...`), de modo que el usuario abra el detalle del recibo con un click.

---

## Estado actual (lo que YA existe — no tocar, reutilizar)

| Componente | Archivo | Estado |
|---|---|---|
| Workflow agentic loop | `nexus-orchestration/src/nexus_orchestration/workflows/rag_query.py` | ✅ max_iterations=5, tool_use branch, streaming Redis, cita expense_ids |
| Bedrock Converse activity | `nexus-orchestration/src/nexus_orchestration/activities/llm.py` | ✅ sync + streaming, tool spec, fake mode |
| Vector search activity | `nexus-orchestration/src/nexus_orchestration/activities/vector_search.py` | ✅ filtro tenant inyectado por workflow (no por LLM), HYBRID query_type |
| Tool spec vector | `nexus-orchestration/src/nexus_orchestration/tools/search_expenses.py` | ✅ |
| Gold layer | `nexus-medallion/src/gold/expense_audit.py` + `expense_chunks.py` | ✅ particionado por tenant_id, CDF habilitado |
| Vector Search index | `nexus-medallion/src/vector/setup_vector_search.py` | ✅ `nexus_dev.vector.expense_chunks_index`, embedding `databricks-bge-large-en`, TRIGGERED |
| Backend chat API | `backend/src/nexus_backend/api/v1/chat.py` | ✅ POST start + SSE stream, tenant check, `chat_sessions` en Mongo |
| Frontend detalle expense | `frontend/src/app/(auth)/expenses/[id]/page.tsx` | ✅ link destino de citations |
| System prompt | `nexus-orchestration/src/nexus_orchestration/prompts/rag_system.md` | ⚠️ 12 líneas, solo habla de vector |

**Gap principal**: el LLM hoy tiene **una sola tool** (`search_expenses` → vector). No puede responder "¿cuánto gasté con Uber en marzo?" con precisión — la vector search devolverá recibos similares pero no un total agregado.

---

## Diseño — por qué "hybrid" y no "router fijo"

Hay dos patrones posibles:

**A) Router determinista antes del LLM** (NLU → clasifica SQL/semantic → enruta).
- Contra: el clasificador se equivoca y no hay forma de combinar las dos fuentes en el mismo turno. Agrega un punto de fallo que no gana nada.

**B) Tool-use nativo del LLM** (dos tools, el modelo decide por turno, puede llamar ambas).
- Pro: Claude ya está entrenado para eso. Se autocorrige (si la SQL devuelve vacío, puede llamar vector). El loop `max_iterations=5` ya lo soporta.
- Pro: combinaciones naturales — "dame mis últimos gastos de comida" puede ser SQL (filtro categoría) + vector (fuzzy sobre vendor "comida").

**Decisión: B.** Es lo que significa "agentic RAG": el agente decide. Nuestro workflow ya tiene la estructura; solo hay que agregar la segunda tool + actualizar el system prompt.

---

## Arquitectura

```
                ┌─────────────────────────────────────┐
                │  Frontend /chat (nuevo)             │
                │   POST /v1/chat → workflow_id       │
                │   GET  /v1/chat/stream/{wf} (SSE)   │
                └──────────────┬──────────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │ Temporal: RAG task   │
                    │ queue nexus-rag-tq   │
                    │ RAGQueryWorkflow     │
                    └──────────┬──────────┘
                               │
         ┌─────────────────────┼──────────────────────┐
         │                     │                      │
         ▼                     ▼                      ▼
  ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
  │ load_chat_   │     │ bedrock_     │     │ save_chat_   │
  │ history      │     │ converse     │     │ turn         │
  │ (Mongo)      │     │ (stream)     │     │ (Mongo)      │
  └──────────────┘     └──────┬───────┘     └──────────────┘
                              │ tool_use?
              ┌───────────────┴──────────────┐
              │                              │
              ▼                              ▼
   ┌────────────────────┐        ┌────────────────────────┐
   │ search_expenses_   │        │ search_expenses_       │
   │ semantic (existente)│       │ structured (NUEVO)      │
   │                    │        │                        │
   │ Databricks VS      │        │ Databricks SQL         │
   │ gold.expense_chunks│        │ gold.expense_audit     │
   │ filter tenant      │        │ WHERE tenant_id = :t   │
   └────────────────────┘        └────────────────────────┘
              │                              │
              └──────────────┬───────────────┘
                             ▼
              assistant message + citations
              [{expense_id, vendor, amount, date, link}]
                             │
                             ▼
                      publish chat.complete
                      → frontend render bubble + citation chips
```

**Invariantes que se preservan**:
- `tenant_id` se inyecta desde el input del workflow en cada activity, NUNCA desde lo que diga el LLM (igual que hoy en `vector_search.py:22`).
- No retry sobre `bedrock_converse` cuando `stream_to_redis=True` (tokens duplicados). Ya está así (`rag_query.py:76`).
- `max_iterations=5` corta loops infinitos.

---

## Work items

### D.1 — Nueva tool: `search_expenses_structured` (SQL sobre gold)

**Nuevo archivo**: `nexus-orchestration/src/nexus_orchestration/tools/search_expenses_structured.py`

```python
SEARCH_EXPENSES_STRUCTURED_TOOL = {
    "name": "search_expenses_structured",
    "description": (
        "Busca gastos por filtros EXACTOS: vendor, categoria, rango de monto, "
        "rango de fechas, moneda. Úsala cuando la pregunta tiene datos concretos "
        "('cuánto gasté en Uber', 'recibos entre $100 y $500 en marzo'). "
        "Devuelve el total agregado + lista de expenses con expense_id para citar."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "vendor":       {"type": "string",  "description": "Match exacto (case-insensitive) sobre vendor final"},
            "category":     {"type": "string",  "description": "travel|food|lodging|office|other"},
            "amount_min":   {"type": "number"},
            "amount_max":   {"type": "number"},
            "date_from":    {"type": "string",  "description": "YYYY-MM-DD inclusive"},
            "date_to":      {"type": "string",  "description": "YYYY-MM-DD inclusive"},
            "currency":     {"type": "string",  "description": "COP|USD|EUR"},
            "aggregate":    {"type": "string",  "enum": ["sum", "count", "list"],
                             "default": "list",
                             "description": "sum/count devuelven total; list devuelve hasta 20 filas"},
            "limit":        {"type": "integer", "default": 20, "maximum": 50}
        },
        "required": []
    }
}
```

Al menos un filtro debe estar presente (la activity valida; si `input == {}` devuelve error para que el LLM reintente).

---

### D.2 — Nueva activity: `search_expenses_structured` (Databricks SQL)

**Nuevo archivo**: `nexus-orchestration/src/nexus_orchestration/activities/sql_search.py`

Responsabilidades:
1. Recibir `{**tool_input, tenant_filter}` — tenant SIEMPRE desde el workflow.
2. Construir SQL parametrizado (no f-string concat) contra `${catalog}.gold.expense_audit`.
3. Ejecutar en un thread (conector Databricks es sync, ya hay precedente en `query_expense.py:90`).
4. Devolver:
   ```json
   {
     "aggregate_kind": "sum" | "count" | "list",
     "aggregate_value": 1234.56,
     "currency": "COP",
     "rows": [
       {"expense_id": "exp_01KQ...", "vendor": "Uber", "amount": 24395.0,
        "currency": "COP", "date": "2026-03-14", "category": "travel"}
     ],
     "row_count_total": 3
   }
   ```

**Guardrails de seguridad**:
- Whitelist de columnas filtrables y de aggregates. **No aceptar `sql_raw` ni columnas arbitrarias del LLM**.
- `WHERE tenant_id = %(tenant_id)s` siempre presente — si la función no lo añade, lanzar `AssertionError`.
- `LIMIT` forzado (max 50), aunque el LLM pida más.
- String comparisons: `LOWER(final_vendor) = LOWER(%(vendor)s)` para match case-insensitive.
- `amount` en COP vs USD: no convertir moneda en SQL — devolver la moneda y que el LLM lo muestre claro.

**Fake mode** (`settings.use_fake_sql_search = settings.fake_providers`):
- Nuevo helper `fake_sql_search()` en `activities/_fakes.py` que devuelve 1-3 rows deterministic por tenant para tests E2E sin Databricks.

---

### D.3 — Extender vector search: siempre incluir `expense_id` en el return

Ya devuelve `expense_id` (ver `vector_search.py:38`, `columns=["chunk_id","expense_id",...]`). ✅ nada que hacer aquí salvo **agregar el `/expenses/{expense_id}` deep link en el return** para que el LLM no tenga que componerlo. Cambio minúsculo en `vector_search.py` al armar la salida:

```python
for row in raw_rows:
    row["link"] = f"/expenses/{row['expense_id']}"
```

Mismo tratamiento en `sql_search.py` rows.

---

### D.4 — RAGQueryWorkflow: registrar la segunda tool y el segundo dispatch

Cambios en `workflows/rag_query.py`:

1. Import del nuevo tool spec:
   ```python
   from nexus_orchestration.tools.search_expenses import SEARCH_EXPENSES_TOOL
   from nexus_orchestration.tools.search_expenses_structured import SEARCH_EXPENSES_STRUCTURED_TOOL
   ```
2. Pasar ambos a Bedrock:
   ```python
   "tools": [SEARCH_EXPENSES_TOOL, SEARCH_EXPENSES_STRUCTURED_TOOL],
   ```
3. En el tool_use branch, añadir la rama del nuevo tool:
   ```python
   elif block["name"] == "search_expenses_structured":
       search_result = await workflow.execute_activity(
           "search_expenses_structured",
           {**block["input"], "tenant_filter": tenant_id},
           start_to_close_timeout=timedelta(seconds=15),
           retry_policy=RetryPolicy(maximum_attempts=3),
       )
       tool_results.append({...})
   ```
4. `_extract_citations_from_history` ya recorre todos los `tool_result.json`. Se deja pasar cualquier estructura con `expense_id`. Ajustar para aplanar: el SQL tool devuelve `{rows: [...]}`, el vector tool devuelve `[...]` directo → función normaliza a lista de `{expense_id, vendor, amount, date, link}`.

---

### D.5 — `main_worker.py`: registrar la nueva activity

Añadir import + append al `activities` list que pasa al worker.

```python
from nexus_orchestration.activities.sql_search import search_expenses_structured
...
activities=[
    ...
    vector_similarity_search,
    search_expenses_structured,   # nuevo
    ...
]
```

---

### D.6 — System prompt: instrucciones para elegir tool

Reescribir `nexus-orchestration/src/nexus_orchestration/prompts/rag_system.md`:

```markdown
Eres el asistente financiero de Nexus. Respondes preguntas del usuario sobre sus
gastos (solo los suyos; nunca hables de otro tenant).

Tienes DOS herramientas:

1. `search_expenses_structured` — búsqueda con filtros exactos sobre la tabla
   oficial de gastos aprobados. Úsala cuando la pregunta menciona:
     • un vendor concreto ("Uber", "Starbucks")
     • una categoría, una moneda, un rango de fechas o montos
     • un total/suma/count ("cuánto gasté", "cuántos recibos")

2. `search_expenses_semantic` — búsqueda semántica por similitud textual sobre
   la descripción del recibo. Úsala cuando la pregunta es difusa o temática:
     • "gastos relacionados con viajes"
     • "recibos que hablen de comida saludable"
     • cuando `search_expenses_structured` devolvió vacío y la pregunta tiene
       matices que un filtro exacto no captura

Puedes llamar las dos en el mismo turno si ayuda.

Reglas de respuesta:
- Números, fechas y vendors deben venir LITERALMENTE del tool_result. No
  inventes valores. Si no hay datos, di "No encontré gastos que coincidan".
- Al final de la respuesta, cita los expenses con el formato exacto:
      [ver recibo](/expenses/{expense_id})
  Una línea por expense citado, máximo 5.
- Sé conciso: 1-3 oraciones de resumen + las citas.
- Si la pregunta es ambigua ("¿gasté mucho?"), pide aclaración antes de llamar
  una tool.
```

**Nota sobre el nombre**: renombrar el tool viejo de `search_expenses` → `search_expenses_semantic` por simetría y claridad. Cambio en:
- `tools/search_expenses.py` (constante)
- `workflows/rag_query.py` (el `elif block["name"] == "search_expenses_semantic"`)
- System prompt.

---

### D.7 — Schema de respuesta: citations con links

`workflows/rag_query.py` ya publica `chat.complete` con `{final_text, citations}`. Ajuste:

**Normalizar cada citation** antes de publicar:
```python
def _normalize_citation(raw: dict) -> dict:
    expense_id = raw["expense_id"]
    return {
        "expense_id": expense_id,
        "vendor":     raw.get("vendor"),
        "amount":     raw.get("amount"),
        "currency":   raw.get("currency"),
        "date":       raw.get("date"),
        "link":       f"/expenses/{expense_id}",
        "source":     raw.get("_source", "unknown"),  # "sql" | "semantic"
    }
```

Dedupe por `expense_id` preservando el primer `source` visto (si el LLM llamó ambas tools, un mismo expense puede aparecer dos veces).

---

### D.8 — Frontend: ruta `/chat` con streaming

**Nueva ruta**: `frontend/src/app/(auth)/chat/page.tsx`.

UI mínimo funcional:
- Input de pregunta + botón send.
- Render del historial de turnos (cargar `GET /v1/chat/sessions/{id}` — endpoint nuevo; D.9).
- Al enviar: `POST /v1/chat` → recibe `{workflow_id, session_id}` → abre `EventSource /v1/chat/stream/{workflow_id}`.
- Durante el stream, va apendeando `chat.token.payload.token` a la burbuja del asistente.
- Al recibir `chat.complete`, renderiza debajo de la burbuja un bloque **Evidencia** con chips clickables: `<Link href={c.link}>{vendor} · {amount} {currency} · {date}</Link>`.
- Persistir `session_id` en query param o localStorage para poder seguir la conversación.

Componente reutilizable: `frontend/src/components/chat/CitationChip.tsx` — badge con icono de recibo, al click navega a `/expenses/{id}`.

---

### D.9 — Backend: endpoint histórico de sesión

Hoy `backend/src/nexus_backend/api/v1/chat.py` solo crea sesión y hace stream. Falta:

`GET /v1/chat/sessions/{session_id}` — devuelve los turnos persistidos por `save_chat_turn` en `chat_turns` de Mongo. Tenant check. Sirve para recargar el historial en el frontend.

Response shape:
```json
{
  "session_id": "sess_01KQ...",
  "turns": [
    {"turn": 1, "user_message": "...", "assistant_message": "...",
     "citations": [{"expense_id": "...", "link": "...", "vendor": "..."}],
     "created_at": "2026-04-24T..."}
  ]
}
```

---

### D.10 — Tests

**Unit (sin Temporal, sin Databricks)**:
- `tests/unit/test_sql_search_builder.py` — verifica que:
  - filtro `tenant_id` siempre incluido
  - `LIMIT` forzado ≤ 50
  - SQL injection attempt en `vendor` no cambia el SQL (parámetros, no concat)
  - aggregate=sum devuelve un solo row; list devuelve varias
- `tests/unit/test_citation_normalizer.py` — dedupe, link format, source tagging.

**Workflow (fake providers)**:
- `tests/workflows/test_rag_hybrid.py`:
  1. Pregunta "¿cuánto gasté en Uber en marzo?" → fake Bedrock devuelve `tool_use: search_expenses_structured {vendor: Uber, date_from: 2026-03-01, date_to: 2026-03-31, aggregate: sum}` → fake SQL devuelve `{sum: 50000 COP, rows: [...]}` → Bedrock segundo turno devuelve texto + stop_reason=end_turn → assert citations len>0, links bien formados.
  2. Pregunta "¿qué compré de comida la semana pasada?" → tool_use vector → citations desde vector result.
  3. Pregunta "gastos de comida" → LLM llama ambas tools en el mismo turno → dedupe por expense_id.
  4. Tool error (SQL devuelve `{error: "no filters provided"}`) → LLM debe reintentar con filtros, no crashear.

**E2E (solo manual, contra Databricks real)**:
- Runbook corto en `infra/plans/phase-d-runbook.md` (separado, se crea tras validar unit+workflow).

---

### D.11 — Observabilidad

Añadir a `observability/`:
- Log estructurado por cada tool call: `{workflow_id, tool, latency_ms, row_count, error?}`.
- Contador Prometheus `rag_tool_calls_total{tool="sql|semantic", outcome="ok|error|empty"}`.
- Span OpenTelemetry por activity (ya existe el tracer; solo propagar atributos nuevos).

---

## Orden de ejecución sugerido

1. **D.6** (rename + prompt) — cero dependencias, baseline de docs.
2. **D.1 + D.2 + fake** — tool spec + activity SQL + fake. Tests unit verdes.
3. **D.3** — enrichment con `link` en ambos activities.
4. **D.5 + D.4** — registrar en worker y workflow.
5. **D.7** — normalización de citations.
6. **D.10** — tests de workflow con fakes, aquí se valida el loop híbrido.
7. **D.9** — endpoint histórico backend.
8. **D.8** — frontend.
9. **D.11** — observability (puede ir en paralelo a partir del paso 4).

Bloqueante crítico: **nada sale a prod sin D.10 verde**, especialmente el test de SQL injection y el test de tenant isolation (prompt malicioso intentando cambiar `tenant_id` en los args de tool).

---

## Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| LLM alucina un `expense_id` que no existe | Citations se arman desde el tool_result, no del texto del LLM. El link apunta a un ID real o no aparece. |
| Inyección de prompt cambia `tenant_id` en tool args | Workflow sobrescribe `tenant_filter` con el del JWT siempre (ya es así en vector, se replica en SQL). |
| SQL injection via vendor/category | Consulta parametrizada + whitelist de columnas. Nunca f-string. |
| Loop infinito (LLM llama tools sin converger) | `max_iterations=5` ya existe. Al excederse → chat.complete con error "no pude resolver". |
| Streaming duplicado en reintentos | `RetryPolicy(maximum_attempts=1)` en `bedrock_converse` cuando stream activo (ya está). |
| Vector index desfasado vs gold reciente | `pipeline_type=TRIGGERED`; documentar runbook: re-sync manual tras approvals masivas. |
| Pregunta ambigua consume 5 iteraciones | System prompt obliga a pedir aclaración si la pregunta no es accionable. |

---

## Fuera de alcance (fases siguientes)

- **Fine-tune / RAG evaluation harness** (precision@k sobre vector) — Phase E.
- **Multi-turno con referencia cruzada** ("y del anterior, ¿cuál era más caro?") — ya funciona parcialmente vía `load_chat_history`; medición y ajuste en Phase E.
- **Summarization de sesiones largas** (context window management) — Phase E.
- **Ingest de políticas internas como corpus adicional** — Phase F.
- **Tool para `SELECT * WHERE similar_to(X)`** (Databricks feature preview) — evaluar en Phase F.

---

## Checklist de definición de "done"

- [ ] `/chat` en frontend renderiza tokens streaming + citations clickables que abren `/expenses/{id}`.
- [ ] Pregunta con filtros exactos llama `search_expenses_structured` (verificable en logs).
- [ ] Pregunta difusa llama `search_expenses_semantic`.
- [ ] Pregunta mixta puede llamar ambas en el mismo turno.
- [ ] `tenant_id` nunca sale del JWT del backend → workflow → activity (test adversarial verde).
- [ ] SQL injection adversarial test verde.
- [ ] `chat_turns` persiste assistant_message + citations por turno.
- [ ] `GET /v1/chat/sessions/{id}` reconstruye el historial para reabrir conversación.
- [ ] Métrica `rag_tool_calls_total` se emite con tool y outcome.
