# Nexus Orchestration — Temporal Workers

Temporal workers that orchestrate the expense audit workflow and the RAG chatbot.

See the spec at the repo root: `03-orquestacion-temporal.md`.
See the shared contracts at `00-contratos-compartidos.md`.

## Prerequisites

The backend's dev stack provides Temporal, MongoDB, Redis, and LocalStack S3.
Start it first:

```bash
cd ../backend
docker compose -f docker-compose.dev.yml up -d
```

## Run the workers (docker)

```bash
docker compose -f docker-compose.workers.yml up --build
```

This launches four containers, one per task queue:
- `worker-orchestrator` → `nexus-orchestrator-tq` (parent `ExpenseAuditWorkflow`)
- `worker-ocr` → `nexus-ocr-tq` (`OCRExtractionWorkflow` + Textract activities)
- `worker-databricks` → `nexus-databricks-tq` (`AuditValidationWorkflow` + Silver/Mongo query)
- `worker-rag` → `nexus-rag-tq` (`RAGQueryWorkflow` + Bedrock streaming + Vector Search)

## Run a single worker locally

```bash
uv sync
TASK_QUEUE=all \
TEMPORAL_HOST=localhost:7233 \
MONGODB_URI='mongodb://localhost:27017/?replicaSet=rs0&directConnection=true' \
REDIS_URL=redis://localhost:6379/0 \
FAKE_PROVIDERS=true \
AUDIT_SOURCE=mongo \
uv run python -m nexus_orchestration.main_worker
```

`TASK_QUEUE=all` registers every workflow and activity in one process. Use the
real task-queue names (`nexus-orchestrator-tq`, etc.) to split by role.

## Tests

```bash
uv run pytest tests/unit -v           # pure units
uv run pytest tests/workflows -v      # WorkflowEnvironment.from_local_time_skipping
uv run pytest tests/integration -v    # contract tests
```

## Fake providers

`FAKE_PROVIDERS=true` replaces Textract, Bedrock, and Databricks Vector Search
with deterministic stubs so the workers run end-to-end against the backend's
dev stack without any external AWS / Databricks credentials. Switch to `false`
when deploying to staging/prod and set the real credentials.

## Integration with the backend

- The backend's `FakeTemporalBackend` (activated with `TEMPORAL_MODE=fake`)
  continues to work for backend-only tests.
- When workers are running with `TEMPORAL_MODE=real` on the backend side, the
  fake is bypassed and the real workflows execute.
- No cross-imports between `nexus_backend` and `nexus_orchestration`. Schemas
  are duplicated and kept in sync via `tests/integration/test_contract_schemas.py`.
