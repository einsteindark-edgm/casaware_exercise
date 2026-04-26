# Autenticación, Autorización y Tenant Isolation — Nexus

> **Objetivo**: documentar el sistema de identidad punta a punta — login en Next.js, intercambio con Cognito, validación de JWT en FastAPI, y propagación del `tenant_id` a través de Mongo, Temporal y Redis.
>
> **Convenciones**: rutas absolutas relativas a `/Users/edgm/Documents/Projects/caseware/casaware_exercise`. Las líneas son aproximadas y reflejan el estado del repo a la fecha de la auditoría.

---

## 0. Vista de 30 segundos

```
                  ┌─────────────── Cognito User Pool ───────────────┐
                  │  custom:tenant_id  · custom:role                │
                  │  USER_SRP_AUTH · OAuth code + JWKS RS256        │
                  └──▲──────────────────────────────────────────────┘
                     │  signIn(email, pwd) / Authorization Code Flow
                     │
┌────────── FRONTEND (Next.js + AWS Amplify) ──────────┐
│  /login        → Auth.signIn(USER_SRP_AUTH)          │
│  /auth/callback → Amplify intercambia code → idToken │
│  useAuth()     → parsea claims (tenantId, role)      │
│  apiClient (ky) beforeRequest                        │
│      └── Authorization: Bearer <idToken>             │
└────────────────────────┬─────────────────────────────┘
                         │  HTTPS (ALB)
                         ▼
┌──────────────── BACKEND (FastAPI) ────────────────────┐
│  Depends(get_current_user)                            │
│    └─ validate_token(jwt)                             │
│         dev  → HS256 con DEV_JWT_SECRET               │
│         prod → RS256 con JWKS cacheado de Cognito     │
│         valida iss, aud, token_use="id"               │
│    └─ devuelve CognitoUser{ sub, email, tenant_id,    │
│                              role, groups }           │
│  Todos los handlers usan user.tenant_id como filtro   │
└────┬──────────────────────────────────────────────────┘
     │ tenant_id viaja embebido en:
     ├─ S3 keys      tenant=<id>/user=<sub>/...
     ├─ Mongo docs   { tenant_id: ... }   (filtro obligatorio)
     ├─ Temporal     workflow input + activity input
     └─ Redis SSE    nexus:events:tenant:<id>:...
```

---

## 1. Frontend (Next.js + AWS Amplify)

### 1.1 Página de login
| Item | Valor |
|---|---|
| Archivo | `frontend/src/app/login/page.tsx` |
| SDK | `aws-amplify/auth` (Amplify v6) |
| Modo dev/prod | `isDevAuth()` (línea 33) |
| Sign-in (prod) | líneas 39–60 — `signIn({ username, password, options: { authFlowType: "USER_SRP_AUTH" } })` |
| Sign-in (dev) | usa `getDevToken()` directo, sin Cognito |

> **SRP** (Secure Remote Password) es preferido sobre `USER_PASSWORD_AUTH` porque la contraseña nunca viaja al servidor.

### 1.2 Configuración de Amplify
| Item | Valor |
|---|---|
| Archivo | `frontend/src/lib/auth/amplify-config.ts` |
| Variables (build-time) | `NEXT_PUBLIC_COGNITO_USER_POOL_ID`, `NEXT_PUBLIC_COGNITO_APP_CLIENT_ID`, `NEXT_PUBLIC_COGNITO_DOMAIN`, `NEXT_PUBLIC_APP_URL` |
| OAuth flow | `["code"]` (Authorization Code Flow) |
| Scopes | `["openid", "email", "profile"]` |
| Redirect signin | `${appUrl}/auth/callback` |
| Redirect signout | `${appUrl}/login` |

### 1.3 Callback de OAuth
| Item | Valor |
|---|---|
| Archivo | `frontend/src/app/auth/callback/page.tsx` |
| Comportamiento | Amplify intercepta el `?code=…&state=…`, intercambia por tokens, dispara evento `signedIn`, redirige a `/` |
| Manejo de errores | listener de `Hub` con `signInWithRedirect_failure` redirige a `/login?error=...` |

### 1.4 Hook `useAuth` y extracción de claims
| Item | Valor |
|---|---|
| Archivo | `frontend/src/lib/auth/use-auth.ts` |
| Tipo | `User { sub, email, tenantId, role }` (líneas 8–13) |
| Prod | `fetchAuthSession()` → toma `tokens.idToken.payload`; `tenantId = payload["custom:tenant_id"]`, `role = payload["custom:role"]` (líneas 38–48) |
| Dev | llama `getDevToken()` y parsea el HS256 (líneas 20–35) |

### 1.5 Token de desarrollo (HS256)
| Item | Valor |
|---|---|
| Archivo | `frontend/src/lib/auth/dev-token.ts` |
| Storage | `localStorage["nexus:dev:id_token"]` |
| `isDevAuth()` | `NEXT_PUBLIC_AUTH_MODE === "dev"` o `?fakeAuth=true` |
| Endpoint que lo emite | `GET /api/v1/dev/token` del backend |
| Auto-refresh | si quedan menos de 60 s para `exp`, refetch al backend |

### 1.6 Cliente HTTP — adjunta Bearer
| Item | Valor |
|---|---|
| Archivo | `frontend/src/lib/api/client.ts` |
| HTTP lib | `ky` |
| Hook | `beforeRequest` (líneas 19–30) |
| Header | `Authorization: Bearer ${idToken}` (línea 24) |
| Token resolver | `getBearerToken()` — dev: `getDevToken()`; prod: `fetchAuthSession()` |

> **Resumen**: el `idToken` (no el access token) es el que viaja al backend, porque las custom-claims de tenant están en el idToken.

---

## 2. Cognito (Terraform)

### 2.1 User Pool
| Item | Valor |
|---|---|
| Archivo | `infra/terraform/cognito.tf` |
| Resource | `aws_cognito_user_pool` (líneas 18–57) |
| Username attr | `email` |
| Auto-verified | `email` |
| Política de contraseña | 8+ chars, mayús/minús/número (líneas 21–28) |
| Custom attribute | `tenant_id` (String, mutable, 1–64 chars) — schema líneas 37–47 |

> **Nota operativa**: el `custom:tenant_id` **no se setea en signup**; se asigna manualmente con `aws cognito-idp admin-update-user-attributes` durante el onboarding del tenant (Phase A del runbook).

### 2.2 App Client
| Item | Valor |
|---|---|
| Resource | `aws_cognito_user_pool_client` (líneas 59–105) |
| Tipo | **Public** (`generate_secret = false`), apto para SPA |
| OAuth flows | `["code"]` |
| Scopes | `["openid", "profile", "email"]` |
| Callback URLs | `http://localhost:3000/auth/callback` (dev), `https://${var.cognito_callback_domain}/auth/callback` (prod) |
| Auth flows explícitos | `ALLOW_USER_SRP_AUTH`, `ALLOW_REFRESH_TOKEN_AUTH`, `ALLOW_ADMIN_USER_PASSWORD_AUTH` |
| Token TTL | idToken 1 h · access 1 h · refresh 30 días |
| Prevent user existence errors | `ENABLED` |

### 2.3 Hosted UI domain
| Item | Valor |
|---|---|
| Resource | `aws_cognito_user_pool_domain` (líneas 107–110) |
| Dominio | `${var.prefix}-auth` |

### 2.4 Variables relevantes
- `infra/terraform/variables.tf` línea 12–16 — `cognito_callback_domain` se rellena después con el DNS del ALB (apply en dos fases para romper la circularidad).

---

## 3. Backend — modelos y validación de JWT

### 3.1 `CognitoUser` (modelo Pydantic)
| Item | Valor |
|---|---|
| Archivo | `backend/src/nexus_backend/auth/models.py` |
| Líneas | 6–16 |
| Config | `populate_by_name=True` (admite alias) |
| Campos | `sub`, `email`, `tenant_id` (alias `custom:tenant_id`), `role` (alias `custom:role`, default `"user"`), `groups` (alias `cognito:groups`, default `["nexus-users"]`) |

> Los **alias** son la clave: Pydantic sabe leer `custom:tenant_id` aunque el atributo Python se llame `tenant_id`.

### 3.2 `TenantContext`
| Item | Valor |
|---|---|
| Archivo | mismo módulo, líneas 18–21 |
| Campos | `tenant_id`, `user_id` |
| Uso | bind de logging y tracing (X-Ray, structlog) |

### 3.3 Validación de JWT
| Item | Valor |
|---|---|
| Archivo | `backend/src/nexus_backend/auth/cognito.py` |
| `_JwksCache` | líneas 18–27 — `keys_by_kid` + `fetched_at`, TTL 6 h (`JWKS_TTL` línea 15) |
| `is_jwks_ready()` | línea 30–34 — usado en `/readyz` |
| `prime_jwks()` | línea 37–41 — invocado en lifespan |
| `_refresh_jwks()` | líneas 44–50 — `GET settings.cognito_jwks_url`, indexa por `kid` |
| `validate_token(token)` | líneas 62–70 — dispatcher dev/prod |
| Dev path | líneas 73–87 — `jwt.decode(token, DEV_JWT_SECRET, algorithms=["HS256"])`, `token_use == "id"` |
| Prod path | líneas 90–119 — lee `kid` del header, busca clave RS256, valida `audience = settings.cognito_app_client_id`, `issuer = https://cognito-idp.{region}.amazonaws.com/{user_pool_id}`, `token_use == "id"` |

### 3.4 Dependencia `get_current_user`
| Item | Valor |
|---|---|
| Archivo | `backend/src/nexus_backend/auth/dependencies.py` |
| Líneas | 11–24 |
| Pasos | 1) `Header("Authorization")` obligatorio; 2) parsea `Bearer <token>`; 3) `validate_token(token)`; 4) bind de `tenant_id` y `user_id` al contexto de logging |
| Errores | `NotAuthenticated` (401) si falta header, mal formato, o JWT inválido |
| Helper | `get_tenant_id` (líneas 27–31) — reusa la misma dependencia |

### 3.5 Configuración del backend
| Item | Valor |
|---|---|
| Archivo | `backend/src/nexus_backend/config.py` |
| `auth_mode` | `"dev"` \| `"prod"` (línea 18) |
| `dev_jwt_secret` | HS256 secret (línea 19) |
| `cognito_user_pool_id`, `cognito_app_client_id`, `cognito_jwks_url` | líneas 21–23 |
| `cognito_issuer` (property) | línea 53–54 — `https://cognito-idp.{region}.amazonaws.com/{user_pool_id}` |

### 3.6 Errores HTTP estándar
| Item | Valor |
|---|---|
| Archivo | `backend/src/nexus_backend/errors.py` |
| `NotAuthenticated` | líneas 20–22 — HTTP 401, code `"not_authenticated"` |
| `NotAuthorized` | líneas 25–27 — HTTP 403, code `"not_authorized"` |
| Envelope | `{ "error": { "code", "message", "request_id", "details"? } }` (líneas 50–60) |

---

## 4. Backend — emisión de tokens en modo dev

### 4.1 Endpoint `GET /api/v1/dev/token`
| Item | Valor |
|---|---|
| Archivo | `backend/src/nexus_backend/api/v1/dev.py` |
| Líneas | 10–42 |
| Guard | `if settings.auth_mode != "dev": raise 404` (líneas 23–24) |
| Query params | `sub`, `tenant_id`, `email`, `role`, `ttl` (60–86 400 s) |
| Respuesta | `{ id_token, token_type, expires_in, sub, tenant_id, email }` |

### 4.2 Función `mint_dev_jwt`
| Item | Valor |
|---|---|
| Archivo | `backend/tests/fakes/fake_cognito.py` |
| Líneas | 10–32 |
| Algoritmo | HS256 con `DEV_JWT_SECRET` |
| Claims | `sub`, `email`, `custom:tenant_id`, `custom:role`, `cognito:groups`, `token_use="id"`, `iat`, `exp` |

> **Importante**: el dev path replica exactamente la forma del idToken de Cognito (mismos nombres de claim), de modo que el resto del backend no necesita conocer si está en dev o en prod.

---

## 5. Autorización y tenant isolation

El sistema **no** tiene RBAC granular per se: la autorización efectiva descansa en dos pilares:

1. **Pertenencia al tenant** — todo recurso multitenant filtra por `tenant_id` en cada lectura/escritura.
2. **Posesión del `expense_id`/`task_id`** — los IDs son ULIDs no enumerables; combinados con el filtro por tenant cierran el acceso cruzado.

El campo `role` y `groups` están reservados para futuros checks (no hay RBAC implementado en endpoints actuales).

### 5.1 Patrón A — filtro en la query (recomendado)

`POST /v1/expenses` → `backend/src/nexus_backend/api/v1/expenses.py::create_expense` (líneas 71–195):

| Recurso | Tenant en | Línea |
|---|---|---|
| S3 key | `tenant={user.tenant_id}/user={user.sub}/{receipt_id}.{ext}` | 99 |
| `receipts.insert_one` | `"tenant_id": user.tenant_id` | 113 |
| `expenses.insert_one` | `"tenant_id": user.tenant_id` | 127 |
| `expense_events.insert_one` | `"tenant_id": user.tenant_id` | 146 |
| `start_workflow` args | `tenant_id=user.tenant_id` | 165 |
| Canales SSE | `user_channel(tenant_id, user_id)`, `workflow_channel(tenant_id, workflow_id)` | 189 |

`GET /v1/expenses` (líneas 198–229) — query base `{"tenant_id": user.tenant_id}` (línea 207).

`GET /v1/expenses/{id}` (líneas 232–243) — `find_one({"expense_id": ..., "tenant_id": user.tenant_id})` (líneas 237–238). Si pertenece a otro tenant: 404 (no 403, para no filtrar existencia).

### 5.2 Patrón B — validación explícita post-lectura

Algunos endpoints HITL leen primero y validan después:

`backend/src/nexus_backend/api/v1/hitl.py::get_hitl_task` (líneas 45–55):
```python
task = await mongo.db.hitl_tasks.find_one({"task_id": task_id})
if task.get("tenant_id") != user.tenant_id:
    raise NotAuthorized("hitl task belongs to a different tenant")
```

> **Recomendación**: este patrón es más débil que el A. Cualquier nuevo handler debe preferir el filtro en la query (`{"task_id": ..., "tenant_id": user.tenant_id}`).

### 5.3 Tenant en SSE
| Item | Valor |
|---|---|
| Archivo | `backend/src/nexus_backend/services/sse_broker.py` |
| Helpers | `user_channel(tenant_id, user_id)`, `workflow_channel(tenant_id, workflow_id)` (líneas 22–27) |
| Naming | `nexus:events:tenant:{tenant_id}:user:{user_id}` y `nexus:events:tenant:{tenant_id}:workflow:{workflow_id}` |
| Aislamiento | el tenant_id está embebido en el nombre del canal Redis → un suscriptor sólo recibe eventos de su canal |

`GET /v1/events/stream` (`backend/src/nexus_backend/api/v1/events.py`, líneas 42–52) construye el canal con `user.tenant_id` y `user.sub` resueltos por `get_current_user`.

### 5.4 Tenant en Temporal
- `ExpenseAuditWorkflow.run(inp)` recibe `tenant_id` como parte del input (línea 38 de `workflows/expense_audit.py`).
- Cada activity de `nexus-orchestration/.../activities/mongodb_writes.py` filtra por `{expense_id, tenant_id}` en updates (líneas 55–98) y por `{session_id, tenant_id}` en chat history (líneas 160–177).
- Inserts incluyen `tenant_id` (`emit_expense_event` línea 108, `create_hitl_task` línea 125, `save_chat_turn` línea 148).

### 5.5 Tenant en S3
- Bucket `receipts`: prefix `tenant=<id>/user=<sub>/<receipt_id>.<ext>` → políticas IAM pueden granular permisos por prefix.
- Bucket `textract-output`: prefix `tenant=<id>/expense=<id>/<uuid>.json`.

---

## 6. Flujo completo en producción (paso a paso)

1. Usuario abre `/login`.
2. Frontend llama `Auth.signIn(email, password, USER_SRP_AUTH)`.
3. Cognito hace SRP handshake; si pass, redirige (o devuelve directamente, según flow) un `code` de OAuth a `/auth/callback`.
4. Amplify intercambia el `code` por `idToken + accessToken + refreshToken`. Los guarda en almacenamiento Amplify (memoria + storage configurado).
5. `useAuth` lee `idToken.payload` y expone `{ sub, email, tenantId, role }`.
6. Cualquier llamada a la API pasa por el `apiClient` (`ky`), que inyecta `Authorization: Bearer <idToken>`.
7. FastAPI ejecuta `get_current_user`:
   - Valida firma RS256 contra el JWK del `kid` cacheado.
   - Verifica `aud == cognito_app_client_id`, `iss == cognito_issuer`, `token_use == "id"`.
   - Construye un `CognitoUser`.
8. El handler usa `user.tenant_id` en todas las queries Mongo, en el S3 key y al disparar Temporal.
9. Temporal arranca `ExpenseAuditWorkflow` con el `tenant_id` en el input. Cada activity hereda ese filtro al tocar Mongo.
10. Eventos SSE se publican a un canal con tenant_id en el nombre. Sólo el frontend autenticado para ese tenant los recibe.
11. Cuando el `idToken` expira (1 h), Amplify usa el `refreshToken` (30 días) para obtener uno nuevo, transparente para el usuario. Si el refresh falla, `useAuth` redirige a `/login`.
12. Logout: `Auth.signOut()` revoca tokens locales y redirige al `redirectSignOut` configurado.

---

## 7. Modo dev (local) — diferencias

| Aspecto | Dev | Prod |
|---|---|---|
| Algoritmo JWT | HS256 con `DEV_JWT_SECRET` | RS256 con JWKS de Cognito |
| Origen del token | `GET /api/v1/dev/token` del backend | Cognito vía Amplify |
| Storage frontend | `localStorage["nexus:dev:id_token"]` | Amplify storage |
| Refresh | reemito completo cuando quedan <60 s | refresh token grant |
| Validación de `aud`/`iss` | omitida | obligatoria |
| Activación | `AUTH_MODE=dev` (back) + `NEXT_PUBLIC_AUTH_MODE=dev` (front) | `AUTH_MODE=prod` |

> Los **claims son idénticos** (`sub`, `email`, `custom:tenant_id`, `custom:role`, `cognito:groups`, `token_use=id`), de modo que los handlers no distinguen entre ambos modos.

---

## 8. Variables de entorno

### 8.1 Backend (`backend/.env.example`)
```
AUTH_MODE=dev                         # "prod" en producción
DEV_JWT_SECRET=dev-secret-change-me   # HS256 secret en dev
COGNITO_USER_POOL_ID=us-east-1_XXXXX
COGNITO_APP_CLIENT_ID=xxxxx
COGNITO_JWKS_URL=https://cognito-idp.us-east-1.amazonaws.com/us-east-1_XXXXX/.well-known/jwks.json
```

### 8.2 Frontend (`frontend/.env.local`)
```
NEXT_PUBLIC_AUTH_MODE=dev             # "prod" en producción
NEXT_PUBLIC_COGNITO_USER_POOL_ID=us-east-1_XXXXX
NEXT_PUBLIC_COGNITO_APP_CLIENT_ID=xxxxx
NEXT_PUBLIC_COGNITO_DOMAIN=nexus-auth.auth.us-east-1.amazoncognito.com
NEXT_PUBLIC_APP_URL=http://localhost:3000
```

> En CI/CD las variables `NEXT_PUBLIC_*` deben estar disponibles en build-time (Next.js las inyecta en el bundle).

---

## 9. Endpoints públicos y privados

| Endpoint | Auth | Notas |
|---|---|---|
| `GET /healthz` | pública | liveness; no toca auth |
| `GET /readyz` | pública | readiness; usa `is_jwks_ready()` para indicar si el cache RS256 está listo |
| `GET /metrics` | pública | Prometheus; protegida a nivel de red |
| `GET /api/v1/dev/token` | pública (sólo en dev) | 404 en prod |
| Resto bajo `/api/v1/*` | `Depends(get_current_user)` | 401 si falta o JWT inválido |

---

## 10. Mapa de archivos clave

```
frontend/src/
  app/login/page.tsx                   # signIn USER_SRP_AUTH
  app/auth/callback/page.tsx           # OAuth code → tokens (Amplify)
  lib/auth/amplify-config.ts           # Amplify configure() — pool, client, OAuth
  lib/auth/use-auth.ts                 # extracción de claims (sub, tenantId, role)
  lib/auth/dev-token.ts                # token HS256 en dev mode
  lib/api/client.ts                    # ky beforeRequest → Authorization: Bearer

backend/src/nexus_backend/
  auth/models.py                       # CognitoUser, TenantContext
  auth/cognito.py                      # validate_token + JWKS cache
  auth/dependencies.py                 # get_current_user, get_tenant_id
  api/v1/dev.py                        # GET /api/v1/dev/token (dev only)
  errors.py                            # NotAuthenticated 401, NotAuthorized 403
  config.py                            # AUTH_MODE, COGNITO_*, cognito_issuer
  services/sse_broker.py               # canales con tenant_id embebido

backend/tests/fakes/fake_cognito.py    # mint_dev_jwt(...) — HS256

infra/terraform/
  cognito.tf                           # User Pool + App Client + custom:tenant_id
  variables.tf                         # cognito_callback_domain (apply en 2 fases)
```

---

## 11. Buenas prácticas y gotchas

- **Usa siempre el idToken** para llamar al backend; el accessToken NO contiene `custom:tenant_id`.
- **Filtra en la query, no después de leer**: el patrón B (`hitl.py::get_hitl_task`) es legacy; nuevos handlers deben usar `find_one({task_id, tenant_id})`.
- **No expongas existencia cross-tenant**: si la query no encuentra, devuelve 404 (no 403).
- **`prevent_user_existence_errors=ENABLED`** en Cognito impide que el formulario filtre si un email existe.
- **El `custom:tenant_id` se setea fuera de banda** (admin script) — un usuario nuevo sin ese atributo recibirá un JWT sin el claim y `validate_token` fallará al construir `CognitoUser` (Pydantic exige el campo).
- **JWKS cache de 6 h** — si rotas las claves del pool, espera o llama a `prime_jwks()` manualmente.
- **El refresh token de 30 días** se invalida en logout o cambio de password; los tokens existentes seguirán siendo válidos hasta su `exp` (1 h) — para revocación inmediata se necesitaría una blocklist (no implementada).
- **CORS y rate-limit** se aplican en `main.py` antes de `get_current_user`; un request mal originado se corta antes de tocar Cognito.
