# 02 · Frontend — Next.js 15 + Cognito + SSE + Chat RAG

> **Rol en Nexus.** Single Page App que permite al usuario (1) subir recibos y ver el progreso de auditoría en tiempo real, (2) resolver tareas HITL cuando el OCR detecta discrepancias con lo reportado, y (3) conversar con un chatbot RAG sobre sus gastos históricos.
>
> **Pre-requisito de lectura:** [`00-contratos-compartidos.md`](./00-contratos-compartidos.md). El esquema de eventos SSE y los endpoints del backend están definidos ahí y en [`01-backend-fastapi.md`](./01-backend-fastapi.md).

---

## 1. Stack técnico

| Componente | Librería / Versión | Justificación |
|---|---|---|
| Framework | **Next.js 15 (App Router)** | React 19, streaming, buen DX |
| Lenguaje | **TypeScript 5.4+** strict mode | Type safety con los schemas del contrato |
| Estilos | **Tailwind CSS 4** + **shadcn/ui** | UI profesional, tokens consistentes |
| Auth | **AWS Amplify v6** (`aws-amplify/auth`) | Integración Cognito oficial, maneja refresh automático |
| Estado server | **TanStack Query v5** | Fetching, caching, invalidación |
| Estado cliente | **Zustand** | Estado global ligero (user, SSE connection state) |
| Forms | **react-hook-form + zod** | Validación cliente alineada con Pydantic backend |
| SSE | **Native `EventSource`** + wrapper con reconnect | Simple, estándar, suficiente |
| Uploads | **`@uploadcare/react`** o implementación nativa con `fetch` + progress | Progress indicator durante upload |
| Icons | **lucide-react** | Consistencia con shadcn |
| Date | **date-fns** | Ligero, tree-shakable |
| HTTP | **ky** o **axios** | Interceptores para inyectar JWT |
| Testing | **Vitest + @testing-library/react + Playwright** | Unitario + E2E |

**Node.js:** 20+.

---

## 2. Estructura del proyecto

```
nexus-frontend/
├── package.json
├── tsconfig.json
├── next.config.ts
├── tailwind.config.ts
├── .env.local.example
├── src/
│   ├── app/                            # App Router
│   │   ├── layout.tsx                  # Root layout con providers
│   │   ├── page.tsx                    # Dashboard (landing autenticado)
│   │   ├── login/
│   │   │   └── page.tsx                # Cognito Hosted UI redirect o custom form
│   │   ├── (auth)/                     # Group de rutas protegidas
│   │   │   ├── layout.tsx              # AuthGuard + Shell
│   │   │   ├── expenses/
│   │   │   │   ├── page.tsx            # Listado
│   │   │   │   ├── new/page.tsx        # Upload de recibo
│   │   │   │   └── [id]/page.tsx       # Detalle con timeline
│   │   │   ├── hitl/
│   │   │   │   └── [taskId]/page.tsx   # Resolución de discrepancia
│   │   │   └── chat/
│   │   │       └── page.tsx            # Chat RAG
│   │   └── api/                        # No hay BFF, solo proxy opcional
│   │
│   ├── lib/
│   │   ├── auth/
│   │   │   ├── amplify-config.ts       # Configuración Cognito
│   │   │   ├── use-auth.ts             # Hook: user, signIn, signOut
│   │   │   └── token.ts                # getIdToken con refresh auto
│   │   ├── api/
│   │   │   ├── client.ts               # ky/axios con auth interceptor
│   │   │   ├── expenses.ts             # Endpoints tipados
│   │   │   ├── hitl.ts
│   │   │   └── chat.ts
│   │   ├── sse/
│   │   │   ├── event-source.ts         # ReconnectingEventSource con Last-Event-ID
│   │   │   └── use-sse-events.ts       # Hook React
│   │   ├── schemas/
│   │   │   ├── events.ts               # Zod schemas de EventEnvelope
│   │   │   ├── expense.ts
│   │   │   └── chat.ts
│   │   └── utils/
│   │       ├── ulid.ts
│   │       └── format.ts
│   │
│   ├── components/
│   │   ├── ui/                         # shadcn/ui primitives
│   │   ├── shell/                      # Navbar, sidebar, layout
│   │   ├── expenses/
│   │   │   ├── expense-upload-form.tsx
│   │   │   ├── expense-list.tsx
│   │   │   ├── expense-detail.tsx
│   │   │   └── expense-timeline.tsx    # Consume eventos SSE del workflow
│   │   ├── hitl/
│   │   │   ├── field-comparison.tsx    # UI side-by-side user vs OCR
│   │   │   └── hitl-resolver.tsx
│   │   ├── chat/
│   │   │   ├── chat-window.tsx
│   │   │   ├── chat-message.tsx
│   │   │   ├── citations.tsx
│   │   │   └── streaming-token-display.tsx
│   │   └── notifications/
│   │       └── toast-sse-listener.tsx  # Muestra toasts según eventos SSE
│   │
│   └── stores/
│       ├── auth-store.ts
│       └── sse-store.ts                # Estado de la conexión SSE global
│
└── tests/
    ├── unit/
    └── e2e/
```

---

## 3. Configuración de autenticación (Amplify + Cognito)

### 3.1. Configuración

```typescript
// src/lib/auth/amplify-config.ts
import { Amplify } from "aws-amplify";

Amplify.configure({
  Auth: {
    Cognito: {
      userPoolId: process.env.NEXT_PUBLIC_COGNITO_USER_POOL_ID!,
      userPoolClientId: process.env.NEXT_PUBLIC_COGNITO_APP_CLIENT_ID!,
      loginWith: {
        oauth: {
          domain: process.env.NEXT_PUBLIC_COGNITO_DOMAIN!,
          scopes: ["openid", "email", "profile"],
          redirectSignIn: [`${process.env.NEXT_PUBLIC_APP_URL}/auth/callback`],
          redirectSignOut: [`${process.env.NEXT_PUBLIC_APP_URL}/login`],
          responseType: "code",
        },
      },
    },
  },
});
```

### 3.2. Hook `useAuth`

```typescript
export function useAuth() {
  const [user, setUser] = useState<CognitoUser | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchAuthSession()
      .then((session) => {
        const idToken = session.tokens?.idToken;
        if (idToken) {
          setUser({
            sub: idToken.payload.sub as string,
            email: idToken.payload.email as string,
            tenantId: idToken.payload["custom:tenant_id"] as string,
            role: idToken.payload["custom:role"] as string,
          });
        }
      })
      .finally(() => setLoading(false));
  }, []);

  return { user, loading, signOut };
}
```

### 3.3. AuthGuard

El `layout.tsx` de `(auth)` redirige a `/login` si `user` es null. También maneja refresh automático antes de que el ID token expire (Amplify lo hace internamente, pero es útil escuchar `Hub` events de Auth).

### 3.4. Propagación del token a la API

El interceptor de `ky` llama `fetchAuthSession()` antes de cada request y añade `Authorization: Bearer ${idToken.toString()}`. **No access token**, porque el backend espera `token_use: "id"` (ver doc 01 §4).

---

## 4. SSE: el corazón de la reactividad

### 4.1. `ReconnectingEventSource`

El navegador trae `EventSource` nativo pero **no permite headers custom** ni lleva cookies a otro origen por defecto. Hay dos soluciones:

**Opción A (recomendada):** pasar el token como query param a un endpoint backend que lo valide antes de abrir el stream. Limitación: los tokens aparecen en logs de nginx. Mitigar con tokens de corta vida o refirmando.

**Opción B:** usar `@microsoft/fetch-event-source` que soporta headers custom sobre `fetch`.

```typescript
// src/lib/sse/event-source.ts
import { fetchEventSource } from "@microsoft/fetch-event-source";

export interface SSESubscription {
  close(): void;
}

export function subscribeToEvents(params: {
  url: string;
  getToken: () => Promise<string>;
  onEvent: (event: EventEnvelope) => void;
  onError?: (err: unknown) => void;
  lastEventId?: string;
}): SSESubscription {
  const abort = new AbortController();

  fetchEventSource(params.url, {
    signal: abort.signal,
    headers: {
      Accept: "text/event-stream",
      ...(params.lastEventId && { "Last-Event-ID": params.lastEventId }),
    },
    async onopen(response) {
      const token = await params.getToken();
      // fetchEventSource no permite interceptor async antes, se sobreescribe
      // Usar token en primera llamada desde fetch wrapper custom.
    },
    onmessage(msg) {
      const parsed = EventEnvelopeSchema.parse(JSON.parse(msg.data));
      // Guardar lastEventId para reconnect
      localStorage.setItem("sse:lastEventId", msg.id);
      params.onEvent(parsed);
    },
    onerror(err) {
      params.onError?.(err);
      // Throw para que fetchEventSource no reintente ante errores fatales
      if (err.status === 401) throw err;
      // Retornar undefined → retry con backoff exponencial incorporado
    },
  });

  return { close: () => abort.abort() };
}
```

**Nota importante:** en la práctica la implementación debe hacer un wrapper que genere un fetch con header `Authorization` al inicio, porque `fetchEventSource` acepta `fetch` custom. Usar el `fetch` del proyecto que ya añade el JWT.

### 4.2. Hook `useSSEEvents`

```typescript
export function useSSEEvents(options: {
  workflowId?: string;      // Si se pasa, se suscribe al canal del workflow
  enabled?: boolean;
}) {
  const { user } = useAuth();
  const [events, setEvents] = useState<EventEnvelope[]>([]);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    if (!user || options.enabled === false) return;

    const url = options.workflowId
      ? `${API_URL}/api/v1/workflows/${options.workflowId}/stream`
      : `${API_URL}/api/v1/events/stream`;

    const lastEventId = localStorage.getItem(`sse:lastEventId:${url}`) ?? undefined;

    const sub = subscribeToEvents({
      url,
      getToken: async () => (await fetchAuthSession()).tokens!.idToken!.toString(),
      lastEventId,
      onEvent: (event) => setEvents((prev) => [...prev, event]),
    });

    setConnected(true);
    return () => {
      sub.close();
      setConnected(false);
    };
  }, [user, options.workflowId, options.enabled]);

  return { events, connected };
}
```

### 4.3. Conexión SSE global

En el `layout.tsx` de `(auth)` montar un **listener global** que abre la conexión al canal `user` y despacha notificaciones (toasts, badges en navbar). Usar Zustand para exponer el estado:

```typescript
// src/stores/sse-store.ts
interface SSEState {
  connected: boolean;
  recentEvents: EventEnvelope[];
  pendingHITL: HITLTask[];  // Derivado de eventos workflow.hitl_required
}
```

Cuando llega `workflow.hitl_required`, **automáticamente** agregar a `pendingHITL` y mostrar un toast con un botón "Resolver" que navega a `/hitl/{taskId}`.

---

## 5. Pantallas y componentes clave

### 5.1. Upload de recibo (`/expenses/new`)

- Formulario con: `amount`, `currency`, `date`, `vendor`, `category`, `receipt` (file picker con drag&drop).
- Validación Zod cliente (debe coincidir con el schema del backend).
- Al submit:
  1. `POST /api/v1/expenses` con `multipart/form-data`.
  2. Mostrar progress bar de upload (usando `XMLHttpRequest` en lugar de fetch para poder escuchar `onprogress`, o la API `fetch` con `ReadableStream`).
  3. Al recibir `202`, redirigir a `/expenses/{id}` donde el usuario ve el progreso en vivo.

### 5.2. Detalle de gasto (`/expenses/[id]`)

- Al montar: fetch `GET /api/v1/workflows/{workflow_id}/status` para obtener estado inicial.
- Suscribir con `useSSEEvents({ workflowId })`.
- Renderizar un **timeline reactivo**:
  - ⏳ Recibido
  - 🔍 Extrayendo texto con Textract (`workflow.ocr_progress`)
  - ✔️ OCR completado
  - ⚠️ Esperando tu revisión (`workflow.hitl_required`) — con botón "Resolver"
  - ✅ Auditoría completada (`workflow.completed`)
  - ❌ Fallo (`workflow.failed`) — con mensaje

El timeline **no se construye con polling**, solo con los eventos SSE acumulados en el store.

### 5.3. HITL (`/hitl/[taskId]`)

Componente central: `FieldComparison`. Para cada campo en conflicto:

```
┌─────────────────────────────────────────┐
│  Monto                                   │
│                                          │
│  Tú reportaste:      $ 100.00            │
│  OCR detectó:        $ 100.50  (95.2%)   │
│                                          │
│  [ Aceptar OCR ]  [ Mantener mío ]       │
│  [ Editar manualmente: $ _______ ]       │
└─────────────────────────────────────────┘
```

Botones al final:
- "Aprobar todos los cambios aceptados" → `POST /api/v1/hitl/{taskId}/resolve` con `decision: "accept_ocr"` o `"custom"` según las elecciones.
- "Rechazar este recibo" → `decision: "keep_user_value"` para todos los campos.

Tras el POST, mostrar spinner hasta recibir evento SSE `workflow.completed` y redirigir a `/expenses/{id}`.

### 5.4. Chat RAG (`/chat`)

- Sidebar con sesiones anteriores (`GET /api/v1/chat/sessions`).
- Ventana central tipo ChatGPT.
- Al enviar mensaje:
  1. `POST /api/v1/chat` con `{ message, session_id }`. Retorna `{ workflow_id, session_id }`.
  2. Abrir SSE a `/api/v1/chat/stream/{workflow_id}`.
  3. Renderizar tokens `chat.token` progresivamente (efecto typewriter).
  4. Al recibir `chat.complete`, renderizar citations (pills con link al recibo original en Gold).

Citation component:
```tsx
<Citation
  rank={1}
  expenseId={citation.expense_id}
  snippet={citation.snippet}
  score={citation.score}
  onClick={() => router.push(`/expenses/${citation.expense_id}`)}
/>
```

### 5.5. Dashboard (`/`)

- Últimos 10 gastos (tabla).
- Widget "Pendientes de tu revisión" que lista `pendingHITL` del store.
- KPIs básicos: total gastado mes actual, por categoría (llamar endpoint de backend que consulte Gold vía Databricks SQL — fuera de scope del flujo principal).

---

## 6. Schemas Zod (espejo del contrato)

```typescript
// src/lib/schemas/events.ts
import { z } from "zod";

export const EventTypeSchema = z.enum([
  "workflow.started",
  "workflow.ocr_progress",
  "workflow.hitl_required",
  "workflow.completed",
  "workflow.failed",
  "chat.token",
  "chat.complete",
  "ping",
]);

export const EventEnvelopeSchema = z.object({
  schema_version: z.literal("1.0"),
  event_id: z.string(),
  event_type: EventTypeSchema,
  workflow_id: z.string().optional(),
  tenant_id: z.string(),
  user_id: z.string(),
  expense_id: z.string().optional(),
  timestamp: z.string().datetime(),
  payload: z.record(z.unknown()),
});

export type EventEnvelope = z.infer<typeof EventEnvelopeSchema>;

// Discriminated unions para payloads específicos
export const HITLRequiredPayloadSchema = z.object({
  hitl_task_id: z.string(),
  fields_in_conflict: z.array(z.object({
    field: z.string(),
    user_value: z.unknown(),
    ocr_value: z.unknown(),
    confidence: z.number(),
  })),
});
```

---

## 7. Observabilidad cliente

- **Sentry** o **Datadog RUM** para errores JS y trazas de performance.
- Emitir custom events: `sse_connected`, `sse_reconnected`, `hitl_resolved_by_user`.
- Métricas UX: tiempo desde upload hasta primer evento SSE, tiempo desde `hitl_required` hasta resolución.

---

## 8. Variables de entorno (Next.js)

```bash
# .env.local
NEXT_PUBLIC_APP_URL=http://localhost:3000
NEXT_PUBLIC_API_URL=http://localhost:8000

NEXT_PUBLIC_COGNITO_USER_POOL_ID=us-east-1_XXXXX
NEXT_PUBLIC_COGNITO_APP_CLIENT_ID=xxxxx
NEXT_PUBLIC_COGNITO_DOMAIN=nexus-auth.auth.us-east-1.amazoncognito.com

NEXT_PUBLIC_SENTRY_DSN=...    # opcional
```

Solo variables `NEXT_PUBLIC_*` se exponen al browser. **Nunca** poner secrets aquí.

---

## 9. Mocks y desarrollo aislado

- **MSW (Mock Service Worker)** para mockear todos los endpoints del backend.
- **Mock SSE server** con un script Node.js que emita eventos cada N segundos simulando el flujo completo. Útil para demos sin backend real.
- En `dev`, permitir un modo `?fakeAuth=true` en la URL que setea un usuario fake sin pasar por Cognito.

---

## 10. Criterios de aceptación

1. **Login flow**: usuario no autenticado es redirigido a `/login`. Tras login Cognito, vuelve a la ruta solicitada.
2. **Token refresh**: sesión activa de más de 1h no interrumpe la app (Amplify refresca automáticamente).
3. **SSE reconnect**: si el backend reinicia, el cliente reconecta en ≤ 5s y recupera eventos perdidos usando `Last-Event-ID`.
4. **Upload con progress**: el usuario ve porcentaje de upload de un PDF de 5 MB. Si cancela, no se crea workflow.
5. **HITL UX**: desde que llega el evento `workflow.hitl_required` hasta que el usuario puede interactuar con el formulario, pasa ≤ 1 segundo.
6. **Chat streaming**: los tokens aparecen progresivamente a ≥ 30 tokens/seg percibidos (efecto fluido).
7. **Accesibilidad**: navegación completa por teclado, contraste AA, labels correctos. Auditoría con Lighthouse ≥ 90.
8. **Responsive**: funciona en mobile (≥ 375px) y desktop.

---

## 11. Referencias

- Next.js App Router: https://nextjs.org/docs/app
- Amplify v6 Auth: https://docs.amplify.aws/react/build-a-backend/auth/
- `@microsoft/fetch-event-source`: https://github.com/Azure/fetch-event-source
- shadcn/ui: https://ui.shadcn.com/
- TanStack Query: https://tanstack.com/query/latest
