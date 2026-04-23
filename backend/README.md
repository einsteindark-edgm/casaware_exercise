# Nexus Backend

FastAPI perimeter for the Nexus system. Authenticates with Cognito, persists to
MongoDB/S3, starts and signals Temporal workflows, and streams Redis pub/sub
events to the frontend via SSE.

See the full specification in the repo root:
- `01-backend-fastapi.md` — this service.
- `00-contratos-compartidos.md` — shared contracts (JWT, Redis channels, event
  envelope, Temporal signals/queries, MongoDB collections).

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (dependency manager)
- Docker + Docker Compose (for the full dev stack)
- `libmagic` on the host if running outside Docker (`brew install libmagic`
  on macOS)

## Quickstart (Docker, recommended)

```bash
cp .env.example .env
docker compose -f docker-compose.dev.yml up --build
```

This brings up:
- MongoDB 7 (single-node replica set `rs0`, required by Debezium CDC — see doc
  04)
- Redis 7
- LocalStack (S3 only, with buckets auto-created)
- Temporal dev server (+ UI on http://localhost:8233)
- The backend on http://localhost:8000

Verify:

```bash
curl http://localhost:8000/healthz     # 200 always
curl http://localhost:8000/readyz      # 200 when all deps are reachable
curl http://localhost:8000/metrics     # Prometheus scrape endpoint
```

## Running without Docker

```bash
uv sync
uv run uvicorn nexus_backend.main:app --reload
```

You still need MongoDB, Redis, S3 (or LocalStack), and Temporal reachable; set
their URLs in `.env`.

## Authentication in dev

`AUTH_MODE=dev` accepts JWTs signed with HS256 using `DEV_JWT_SECRET`. Generate
a token:

```bash
uv run python scripts/gen_test_jwt.py \
  --sub a3f4-user-1 \
  --tenant t_01HQ_ACME \
  --email test@example.com \
  --role user
```

Use the printed token as `Authorization: Bearer <token>` on all `/api/v1/*`
endpoints.

In prod, set `AUTH_MODE=prod` and the `COGNITO_*` variables; the backend
validates RS256 tokens against the JWKS published at `COGNITO_JWKS_URL`.

## Temporal

`TEMPORAL_MODE=real` (default) connects to `TEMPORAL_HOST`. Set
`TEMPORAL_MODE=fake` to use an in-memory `FakeTemporalClient` — useful before
the workers from doc 03 are built.

## Layout

```
src/nexus_backend/
├── main.py              # FastAPI app, lifespan, middlewares
├── config.py            # pydantic-settings
├── errors.py            # NexusError hierarchy + handlers
├── ulid_ids.py          # ULID helpers (exp_, rcpt_, hitl_, evt_)
├── auth/                # Cognito JWKS + dev HS256
├── api/                 # HTTP routers
│   └── v1/              # /api/v1/*
├── services/            # Mongo, S3, Redis, Temporal, SSE broker
├── schemas/             # Pydantic v2 models matching doc 00 contracts
└── observability/       # structlog, Prometheus, X-Ray
```

## End-to-end smoke test

```bash
TOKEN=$(uv run python scripts/gen_test_jwt.py --sub a3f4 --tenant t_01 --email me@acme.co)

# In one terminal, subscribe to SSE:
curl -N -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/v1/events/stream

# In another, create an expense:
echo '%PDF-1.4 test' > /tmp/test.pdf
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -F file=@/tmp/test.pdf \
  -F 'expense_json={"amount":100.50,"currency":"COP","date":"2026-04-22","vendor":"Starbucks","category":"food"}' \
  http://localhost:8000/api/v1/expenses
```

The SSE terminal should immediately receive a `workflow.started` event.
