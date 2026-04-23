# 01 · Backend — FastAPI + SSE + Redis + Cognito + Temporal Client

> **Rol en Nexus.** Este sistema es el **perímetro HTTP** entre el usuario y toda la infraestructura backend. Recibe peticiones del frontend, autentica contra Cognito, persiste en MongoDB/S3, inicia/consulta workflows en Temporal, y mantiene abiertas conexiones SSE que traducen eventos de Redis a notificaciones reactivas.
>
> **Pre-requisito de lectura:** [`00-contratos-compartidos.md`](./00-contratos-compartidos.md). Los nombres de canales, signals y schemas vienen de ahí.

---

## 1. Stack técnico

| Componente | Librería / Versión | Justificación |
|---|---|---|
| Framework HTTP | **FastAPI ≥ 0.135.0** | Soporte SSE nativo con `EventSourceResponse` desde 0.135 |
| Servidor ASGI | **Uvicorn 0.30+** con workers Gunicorn | Estándar de producción FastAPI |
| Validación | **Pydantic v2** | Schemas, serialización, validación automática |
| Cognito JWT | **python-jose[cryptography] 3.3+** + **httpx** para JWKS | Valida RS256 contra JWKS cacheado |
| Redis async | **redis-py 5.0+** con `redis.asyncio` | Cliente oficial, async nativo |
| MongoDB async | **motor 3.5+** | Driver oficial async de MongoDB |
| S3 async | **aioboto3 13+** | boto3 wrapper async |
| Temporal client | **temporalio 1.8+** | SDK Python oficial |
| Observabilidad | **prometheus-fastapi-instrumentator**, **structlog**, **aws-xray-sdk** | CloudWatch + Prometheus + X-Ray |
| Rate limiting | **slowapi 0.1.9+** | Protección contra abuso |
| Gestión de deps | **uv** | Rápido, moderno, reproducible |

**Python:** 3.12+.

---

## 2. Estructura del proyecto

```
nexus-backend/
├── pyproject.toml
├── uv.lock
├── .env.example
├── Dockerfile
├── docker-compose.dev.yml
├── src/
│   └── nexus_backend/
│       ├── __init__.py
│       ├── main.py                      # Entry point FastAPI app
│       ├── config.py                    # Pydantic BaseSettings
│       ├── auth/
│       │   ├── cognito.py               # JWKS cache + validación JWT
│       │   ├── dependencies.py          # get_current_user, get_tenant_id
│       │   └── models.py                # Pydantic: CognitoUser, TenantContext
│       ├── api/
│       │   ├── v1/
│       │   │   ├── expenses.py          # POST /expenses (upload + start wf)
│       │   │   ├── workflows.py         # GET /workflows/{id}, signals
│       │   │   ├── hitl.py              # POST /hitl/{task_id}/resolve
│       │   │   ├── events.py            # GET /events/stream (SSE)
│       │   │   └── chat.py              # POST /chat, GET /chat/stream
│       │   └── health.py                # /healthz, /readyz, /metrics
│       ├── services/
│       │   ├── mongodb.py               # Repositorios motor
│       │   ├── s3.py                    # Upload presignado + get
│       │   ├── temporal_client.py       # Start/signal/query workflows
│       │   ├── redis_client.py          # Pub/Sub + health
│       │   └── sse_broker.py            # Traduce Redis → EventSourceResponse
│       ├── schemas/
│       │   ├── events.py                # EventEnvelope (del contrato 2.3)
│       │   ├── expense.py               # ExpenseCreate, ExpenseRead
│       │   ├── hitl.py                  # HitlResolution
│       │   └── chat.py                  # ChatMessage, ChatStreamChunk
│       ├── observability/
│       │   ├── logging.py               # structlog config
│       │   ├── metrics.py               # Prometheus custom metrics
│       │   └── xray.py                  # X-Ray segments
│       └── errors.py                    # Jerarquía de excepciones + handlers
└── tests/
    ├── conftest.py
    ├── unit/
    └── integration/
```

---

## 3. Configuración (`config.py`)

Usar `pydantic-settings`. Variables exactamente como en §3 del doc 00.

```python
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "dev"
    aws_region: str = "us-east-1"

    # Cognito
    cognito_user_pool_id: str
    cognito_app_client_id: str
    cognito_jwks_url: str

    # Temporal
    temporal_host: str
    temporal_namespace: str
    temporal_tls_cert_path: str | None = None
    temporal_tls_key_path: str | None = None

    # Redis
    redis_url: str
    redis_tls: bool = True

    # MongoDB
    mongodb_uri: str
    mongodb_db: str

    # S3
    s3_receipts_bucket: str

    # CORS
    cors_origins: list[str] = ["http://localhost:3000"]

    # Rate limiting
    rate_limit_default: str = "100/minute"

settings = Settings()
```

---

## 4. Autenticación con Cognito

### 4.1. Validación JWT contra JWKS

**No** traer el token al backend a ciegas. Implementar:

1. Al arrancar la app, descargar el JWKS desde `COGNITO_JWKS_URL` y cachear en memoria con TTL de 6h.
2. En cada request, extraer el header `Authorization: Bearer <token>`, parsear el `kid` del header JWT, buscar la clave pública correspondiente, y validar con `jose.jwt.decode`:
   - `algorithms=["RS256"]`
   - `audience=settings.cognito_app_client_id`
   - `issuer=f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"`
   - Verificar `token_use == "id"` (rechazar access tokens — no llevan custom claims).

### 4.2. Modelo `CognitoUser`

```python
from pydantic import BaseModel, Field

class CognitoUser(BaseModel):
    sub: str                                    # user_id
    email: str
    tenant_id: str = Field(alias="custom:tenant_id")
    role: str = Field(alias="custom:role")
    groups: list[str] = Field(default_factory=list, alias="cognito:groups")

    model_config = {"populate_by_name": True}
```

### 4.3. Dependencies

```python
async def get_current_user(
    authorization: str = Header(..., alias="Authorization"),
) -> CognitoUser:
    """Valida JWT y retorna CognitoUser. 401 si inválido."""
    # ... validación ...
    return CognitoUser(**payload)

async def get_tenant_id(user: CognitoUser = Depends(get_current_user)) -> str:
    """Shortcut para endpoints que solo necesitan tenant_id."""
    return user.tenant_id
```

**Regla:** TODO endpoint excepto `/healthz` y `/readyz` depende de `get_current_user` o `get_tenant_id`.

---

## 5. Endpoints HTTP — especificación

Prefijo de API: `/api/v1`.

### 5.1. `POST /expenses` — crear gasto y disparar auditoría

**Request:** `multipart/form-data`
- `file`: imagen o PDF del recibo (max 10 MB, tipos: `image/jpeg`, `image/png`, `application/pdf`)
- `expense_json`: string JSON con:
  ```json
  {
    "amount": 100.50,
    "currency": "COP",
    "date": "2026-04-22",
    "vendor": "Starbucks",
    "category": "food"
  }
  ```

**Flujo interno:**

1. Validar JWT → obtener `tenant_id`, `user_id`.
2. Generar `receipt_id = "rcpt_" + ulid()` y `expense_id = "exp_" + ulid()`.
3. Subir archivo a S3 con key `tenant={tenant_id}/user={user_id}/{receipt_id}.{ext}`.
4. Insertar en MongoDB colección `receipts` (metadata).
5. Insertar en MongoDB colección `expenses` con `status="pending"` y `workflow_id="expense-audit-{expense_id}"`.
6. Iniciar workflow en Temporal:
   ```python
   await temporal_client.start_workflow(
       "ExpenseAuditWorkflow",
       args=[ExpenseAuditInput(
           expense_id=expense_id,
           tenant_id=tenant_id,
           user_id=user_id,
           receipt_s3_key=s3_key,
           user_reported_data={...}
       )],
       id=f"expense-audit-{expense_id}",
       task_queue="nexus-orchestrator-tq",
       id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
   )
   ```
7. Retornar `202 Accepted`:
   ```json
   { "expense_id": "exp_...", "workflow_id": "expense-audit-...", "status": "processing" }
   ```

### 5.2. `GET /workflows/{workflow_id}/status` — consultar estado

Usa Temporal **Query** `get_status` (read-only, no cambia estado del workflow).

```python
handle = temporal_client.get_workflow_handle(workflow_id)
status = await handle.query("get_status")
```

Retorna el resultado directo del query (ver §2.4 del contrato).

**Autorización:** validar que el `workflow_id` pertenezca al `tenant_id` del usuario. Consultar MongoDB `expenses` por el `workflow_id` y chequear `tenant_id`.

### 5.3. `POST /hitl/{task_id}/resolve` — resolver discrepancia

**Request body:**
```json
{
  "decision": "accept_ocr" | "keep_user_value" | "custom",
  "resolved_fields": { "amount": 100.50, "date": "2026-04-22" }
}
```

**Flujo:**

1. Cargar `hitl_tasks` de MongoDB por `task_id`. Validar `tenant_id` coincide.
2. Enviar signal `hitl_response` al workflow:
   ```python
   await handle.signal("hitl_response", {
       "resolved_fields": body.resolved_fields,
       "decision": body.decision,
       "user_id": user.sub,
       "timestamp": datetime.utcnow().isoformat(),
   })
   ```
3. Actualizar `hitl_tasks.status = "resolved"` en MongoDB.
4. Retornar `204 No Content`.

### 5.4. `GET /events/stream` — SSE global del usuario

Endpoint SSE que emite eventos del canal `nexus:events:tenant:{tenant_id}:user:{user_id}`.

```python
from fastapi.sse import EventSourceResponse, ServerSentEvent

@router.get("/events/stream", response_class=EventSourceResponse)
async def stream_events(
    request: Request,
    user: CognitoUser = Depends(get_current_user),
    broker: SSEBroker = Depends(get_sse_broker),
) -> AsyncIterable[ServerSentEvent]:
    channel = f"nexus:events:tenant:{user.tenant_id}:user:{user.sub}"
    async for event in broker.subscribe(channel, request):
        yield ServerSentEvent(
            data=event.model_dump_json(),
            event=event.event_type,
            id=event.event_id,
        )
```

**Puntos críticos:**

- **Heartbeat** cada 15 segundos (un evento con `event_type="ping"`) para que proxies no cierren la conexión.
- **Detectar desconexión** del cliente con `await request.is_disconnected()` en cada iteración. Si true, cerrar suscripción Redis.
- **Reconnect con `Last-Event-ID`.** El SSE broker debe buffer en Redis Streams los últimos 50 eventos por usuario (en un Sorted Set con `event_id` como score) y, si el cliente envía `Last-Event-ID`, replicar los perdidos antes de enganchar el pub/sub live. Esto evita pérdida de eventos en reconexiones.

### 5.5. `POST /chat` — iniciar sesión RAG

```json
// request
{ "message": "¿Cuánto gasté en febrero en comida?", "session_id": "sess_..." | null }
```

- Si `session_id` es null, crear uno nuevo y guardarlo en Mongo `chat_sessions`.
- Iniciar `RAGQueryWorkflow` con `workflow_id=f"rag-{session_id}-{turn_n}"`.
- Retornar el `workflow_id` y `session_id`. El frontend abre luego `/chat/stream/{workflow_id}` para recibir tokens.

### 5.6. `GET /chat/stream/{workflow_id}` — SSE de tokens de chat

Se suscribe al canal `nexus:events:tenant:{tenant_id}:workflow:{workflow_id}` y filtra eventos `chat.token` y `chat.complete`.

### 5.7. `GET /expenses` — listar gastos del tenant

Query params: `status`, `date_from`, `date_to`, `cursor`, `limit`.

Lee de **MongoDB `expenses`** (no de Databricks — la capa Silver/Gold es para analítica, MongoDB es la fuente viva).

Aplicar **siempre** filtro `tenant_id = user.tenant_id` server-side.

### 5.8. `GET /expenses/{expense_id}/history` — timeline del gasto

Devuelve el historial semántico del expense para la pantalla de detalle. Lee de la colección **`expense_events`** (definida en doc 00 §2.6), no de Bronze ni de Temporal.

**Por qué `expense_events` y no las otras alternativas:**
- Bronze CDC: dump técnico, granular por campo, requiere query Databricks (~2-5s + costo de cluster). Inviable para una pantalla visitada cientos de veces al día.
- Temporal `workflow.history`: orientado a debugging, contiene signals/activities/retries. Inestable para UX (cambia si cambias el orquestador).
- `expense_events`: alto nivel, en Mongo (~10ms), independiente del orquestador, ya replicado por CDC a Silver para analytics.

**Implementación:**

```python
@router.get("/expenses/{expense_id}/history")
async def get_expense_history(
    expense_id: str,
    user: CognitoUser = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> list[ExpenseEventRead]:
    # 1. Validar que el expense pertenece al tenant del usuario
    expense = await db.expenses.find_one(
        {"expense_id": expense_id, "tenant_id": user.tenant_id},
        {"_id": 0, "expense_id": 1},
    )
    if not expense:
        raise ResourceNotFound(f"expense {expense_id} not found")

    # 2. Leer eventos ordenados cronológicamente
    cursor = db.expense_events.find(
        {"expense_id": expense_id, "tenant_id": user.tenant_id},
        {"_id": 0},
    ).sort("created_at", 1).limit(200)

    events = await cursor.to_list(length=200)
    return [ExpenseEventRead.model_validate(e) for e in events]
```

**Schema `ExpenseEventRead`:** mapea 1:1 a la colección `expense_events` del contrato §2.6.

**Performance:**
- Índice obligatorio: `{tenant_id: 1, expense_id: 1, created_at: 1}` en `expense_events`.
- Límite hard de 200 eventos por expense (un expense típico tiene 5-15 eventos; 200 es buffer para casos patológicos).
- Cache opcional de 60s en Redis para expenses ya finalizados (`status in ['approved', 'rejected']`) — sus eventos no van a cambiar.

**Relación con SSE:** los eventos en `expense_events` son persistencia del mismo timeline que el frontend recibe en vivo por SSE durante el flujo activo. Cuando el usuario abre la pantalla de detalle horas o días después, este endpoint reconstruye el timeline completo. Si el expense aún está activo, el frontend complementa con SSE para ver los eventos nuevos en vivo.

### 5.9. `GET /healthz` y `/readyz`

- `/healthz`: 200 siempre que el proceso esté vivo.
- `/readyz`: chequea conexión a Mongo, Redis, Temporal y Cognito JWKS. 503 si alguno falla.

---

## 6. SSE Broker (componente crítico)

### 6.1. Diseño

```python
class SSEBroker:
    def __init__(self, redis: Redis):
        self._redis = redis

    async def publish(self, channel: str, event: EventEnvelope) -> None:
        """Llamado por otros servicios del backend (p. ej. tras iniciar wf)."""
        await self._redis.publish(channel, event.model_dump_json())
        # Además, guardar en el Sorted Set para replay:
        key = f"{channel}:buffer"
        await self._redis.zadd(key, {event.model_dump_json(): event.epoch_ms})
        await self._redis.zremrangebyrank(key, 0, -51)  # mantener últimos 50
        await self._redis.expire(key, 3600)

    async def subscribe(
        self,
        channel: str,
        request: Request,
        last_event_id: str | None = None,
    ) -> AsyncIterator[EventEnvelope]:
        # 1) Replay de eventos perdidos si last_event_id presente
        if last_event_id:
            buffered = await self._redis.zrangebyscore(
                f"{channel}:buffer",
                min=ulid_to_epoch_ms(last_event_id),
                max="+inf",
            )
            for raw in buffered:
                yield EventEnvelope.model_validate_json(raw)

        # 2) Pub/sub live
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(channel)
        last_heartbeat = time.monotonic()
        try:
            while True:
                if await request.is_disconnected():
                    break
                msg = await pubsub.get_message(timeout=1.0, ignore_subscribe_messages=True)
                if msg and msg["type"] == "message":
                    yield EventEnvelope.model_validate_json(msg["data"])
                # Heartbeat cada 15s
                if time.monotonic() - last_heartbeat > 15:
                    yield EventEnvelope.heartbeat()
                    last_heartbeat = time.monotonic()
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()
```

### 6.2. Consumidores del broker

El backend publica en Redis principalmente desde:
- **Después de `start_workflow`**: publicar `workflow.started` inmediatamente para que el frontend tenga feedback instantáneo (no esperar al primer evento del worker).
- **Después de `hitl_response` signal**: publicar `workflow.hitl_resolved` (confirmación).

Los Workers de Temporal también publican directamente en Redis (ver doc 03).

---

## 7. Observabilidad

### 7.1. Logs (structlog)

Configurar structlog para JSON output. **Todos los logs incluyen** (vía `contextvars`):
- `request_id` (generado en middleware)
- `tenant_id`
- `user_id`
- `workflow_id` (si aplica)

### 7.2. Métricas (Prometheus)

Usar `prometheus-fastapi-instrumentator`. Métricas custom a exponer:

```python
sse_connections_active = Gauge("nexus_sse_connections_active", labelnames=["tenant_id"])
sse_events_published = Counter("nexus_sse_events_published_total", labelnames=["event_type"])
workflow_starts = Counter("nexus_workflow_starts_total", labelnames=["workflow_type", "tenant_id"])
hitl_resolutions = Counter("nexus_hitl_resolutions_total", labelnames=["decision"])
```

Exponer en `/metrics`. Configurar scrape en CloudWatch Agent o en Prometheus directamente.

### 7.3. X-Ray

Envolver el entry de cada request con `xray_recorder.begin_segment`. Propagar el `TraceId` en el header `X-Amzn-Trace-Id` al iniciar workflows Temporal (guardarlo como `Memo` del workflow — ver doc 03).

---

## 8. Manejo de errores

Jerarquía:

```python
class NexusError(Exception): ...
class NotAuthenticated(NexusError): ...        # 401
class NotAuthorized(NexusError): ...            # 403 (tenant_id mismatch)
class ResourceNotFound(NexusError): ...         # 404
class TemporalUnavailable(NexusError): ...      # 503 (retry-after)
class ValidationFailed(NexusError): ...         # 422
```

Handler global traduce a JSON:
```json
{ "error": { "code": "not_authorized", "message": "...", "request_id": "..." } }
```

---

## 9. Seguridad

1. **CORS** limitado a orígenes de `settings.cors_origins`.
2. **Rate limit** por `user.sub` (no por IP, porque múltiples users pueden estar detrás del mismo NAT).
3. **Tamaño máximo** de upload a 10 MB, validar antes de streamear a S3.
4. **MIME sniff**: validar magic bytes del archivo, no confiar en `Content-Type` del cliente. Librería: `python-magic`.
5. **Prohibido** loguear el token JWT completo. Loguear solo `sub` y `tenant_id`.

---

## 10. Mocks y desarrollo aislado

Para desarrollar sin el resto del sistema:

- **Cognito mock**: flag `AUTH_MODE=dev` que acepta cualquier JWT firmado con clave local de pruebas. Generar tokens de test con `scripts/gen_test_jwt.py`.
- **Temporal mock**: clase `FakeTemporalClient` en `tests/fakes/` que registra las llamadas en memoria.
- **MongoDB**: `mongomock-motor` o un contenedor real en `docker-compose.dev.yml`.
- **Redis**: contenedor real (es liviano).

`docker-compose.dev.yml` debe levantar: MongoDB, Redis, LocalStack (S3 + Cognito), Temporal dev server.

---

## 11. Criterios de aceptación

1. **Flujo completo**: usuario autenticado puede subir recibo, recibe `202` con `workflow_id`, y en ≤ 3 segundos ve evento `workflow.started` por SSE.
2. **HITL end-to-end**: cuando el worker emite `workflow.hitl_required`, el frontend recibe el evento en ≤ 500ms. Al hacer `POST /hitl/.../resolve`, el workflow reanuda en ≤ 1 segundo.
3. **SSE resiliente**: desconectar red 10s y reconectar con `Last-Event-ID` → ningún evento se pierde.
4. **Multi-tenant**: usuario del tenant A no puede consultar `workflow_id` del tenant B (403).
5. **Carga**: 100 conexiones SSE concurrentes no degradan latencia de endpoints REST > 100ms p95.
6. **Observabilidad**: cada request tiene `request_id` rastreable en logs + X-Ray + métricas.

---

## 12. Referencias

- FastAPI SSE nativo: https://fastapi.tiangolo.com/tutorial/server-sent-events/
- `sse-starlette` (fallback si FastAPI < 0.135): https://github.com/sysid/sse-starlette
- Cognito JWT verification: https://docs.aws.amazon.com/cognito/latest/developerguide/amazon-cognito-user-pools-using-tokens-verifying-a-jwt.html
- Temporal Python SDK: https://python.temporal.io/
- Redis async: https://redis-py.readthedocs.io/en/stable/examples/asyncio_examples.html
