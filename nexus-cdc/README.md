# nexus-cdc

CDC listener Mongo → S3 para Databricks Autoloader. Reemplaza el notebook batch
`seed_bronze_from_mongo.py` por un pipeline continuo.

> Ver [infra/plans/phase-b.md](../infra/plans/phase-b.md) para el rationale completo.

## Arquitectura

```
MongoDB Atlas  ──(change streams)──► listener (ECS Fargate, 1 task)
                                       │
                                       ▼
                s3://{prefix}-cdc/{collection}/year=.../batch_<ULID>.jsonl.gz
                                       │
                                       ▼           ┌─ silver → gold → vector_search
                Databricks Autoloader (cloudFiles) ─┤
                → bronze.mongodb_cdc_*              └─ (ya existian, sin cambios)
```

Resume tokens en DynamoDB (`nexus-dev-edgm-cdc-offsets`). At-least-once delivery:
duplicados son resueltos por `apply_changes` en silver con `sequence_by=__source_ts_ms`.

## Estructura

- `src/nexus_cdc/config.py` — Settings leídos via env vars.
- `src/nexus_cdc/main.py` — Entry point. Spawn 1 thread por colección + tick driver.
- `src/nexus_cdc/listener.py` — Watch stream + bootstrap full-sync.
- `src/nexus_cdc/batcher.py` — Buffer → S3 JSONL.gz.
- `src/nexus_cdc/checkpoint.py` — DynamoDB resume tokens.
- `src/nexus_cdc/mappers.py` — Mongo BSON → JSON-safe envelope.

## Dev loop local

```bash
cd nexus-cdc
uv sync --group dev
uv run pytest tests -v
```

Para probar contra Mongo real:

```bash
cp .env.example .env
# rellenar MONGODB_URI, S3_BUCKET, DDB_OFFSETS_TABLE con valores de terraform output
uv run python -m nexus_cdc.main
```

## Deploy a AWS

### 1. Levantar la infra

```bash
cd infra/terraform
terraform apply \
  -target=aws_ecr_repository.cdc \
  -target=aws_s3_bucket.cdc \
  -target=aws_s3_bucket_lifecycle_configuration.cdc \
  -target=aws_s3_bucket_public_access_block.cdc \
  -target=aws_s3_bucket_server_side_encryption_configuration.cdc \
  -target=aws_dynamodb_table.cdc_offsets \
  -target=aws_iam_role.cdc_task \
  -target=aws_iam_role_policy.cdc_task \
  -target=aws_cloudwatch_log_group.cdc \
  -target=aws_ssm_parameter.cdc_bucket \
  -target=aws_ssm_parameter.cdc_offsets_table
```

### 2. Build + push imagen

```bash
cd nexus-cdc
AWS_REGION=us-east-1
REPO=$(terraform -chdir=../infra/terraform output -raw cdc_ecr_repository_url)

aws ecr get-login-password --region $AWS_REGION \
  | docker login --username AWS --password-stdin $REPO

docker build --platform linux/amd64 -t $REPO:v1 .
docker push $REPO:v1
```

### 3. Arrancar el servicio

```bash
cd ../infra/terraform
terraform apply \
  -target=aws_ecs_task_definition.cdc \
  -target=aws_ecs_service.cdc \
  -var=cdc_image_tag=v1 \
  -var=cdc_desired_count=1
```

### 4. Verificar

```bash
# Logs
aws logs tail /ecs/nexus-dev-edgm/cdc --follow

# Esperar ~1-2 min. En logs aparecen:
#   {"event": "bootstrap_start", "collection": "expenses", ...}
#   {"event": "batch_flushed", "collection": "expenses", "events": 10, ...}
#   {"event": "bootstrap_complete", "collection": "expenses", ...}
#   {"event": "watch_start", "collection": "expenses", ...}

# Archivos en S3
aws s3 ls s3://nexus-dev-edgm-cdc/expenses/ --recursive | head

# Offsets en DDB
aws dynamodb scan --table-name nexus-dev-edgm-cdc-offsets --max-items 10
```

### 5. Enable bronze_cdc pipeline

```bash
cd ../../nexus-medallion
databricks bundle deploy -t dev
# Arranca el primer run manual:
databricks bundle run bronze_cdc_pipeline -t dev
```

Esperar ~3-5 min. Verificar:

```sql
SELECT COUNT(*), MIN(__source_ts_ms), MAX(__source_ts_ms)
FROM nexus_dev.bronze.mongodb_cdc_expenses;
```

### 6. Test E2E

```bash
# Insert un expense en Mongo desde el backend o shell
mongosh "$MONGODB_URI" --eval '
  db.expenses.insertOne({
    expense_id: "exp_CDC_TEST_1",
    tenant_id: "t_alpha",
    user_id: "u_test",
    amount: 99.99,
    currency: "USD",
    status: "pending",
    created_at: new Date(),
    updated_at: new Date()
  })
'

# En <35s, debe aparecer en S3:
aws s3 ls s3://nexus-dev-edgm-cdc/expenses/ --recursive | tail

# Triggear el pipeline o esperar al cron de 5min
databricks bundle run bronze_cdc_pipeline -t dev

# Verificar bronze:
databricks sql execute -w <warehouse-id> \
  "SELECT __op, __source_ts_ms FROM nexus_dev.bronze.mongodb_cdc_expenses WHERE expense_id='exp_CDC_TEST_1'"

# Despues: silver
databricks bundle run silver_pipeline -t dev
databricks sql execute -w <warehouse-id> \
  "SELECT status, amount FROM nexus_dev.silver.expenses WHERE expense_id='exp_CDC_TEST_1'"
```

## Runbooks

### Resetear un listener (re-bootstrap)

```bash
aws dynamodb delete-item \
  --table-name nexus-dev-edgm-cdc-offsets \
  --key '{"collection":{"S":"expenses"}}'
aws ecs update-service \
  --cluster nexus-dev-edgm \
  --service nexus-dev-edgm-cdc \
  --force-new-deployment
```

El listener reanuda con bootstrap full-sync. Los archivos previos en S3 no se borran
(Autoloader los ignora gracias al checkpoint en `_autoloader_state/`), pero los
eventos nuevos aparecen con `__op=r`.

### Drop + reseed completo

Si hay duplicados extremos o corrupcion:

```bash
# 1. Scale down
aws ecs update-service --cluster nexus-dev-edgm --service nexus-dev-edgm-cdc --desired-count 0

# 2. Borrar bronze tables y state de Autoloader
databricks sql execute "DROP TABLE IF EXISTS nexus_dev.bronze.mongodb_cdc_expenses"
# ... repetir para las 5 bronze tables
aws s3 rm s3://nexus-dev-edgm-cdc/_autoloader_state/ --recursive

# 3. Borrar offsets
aws dynamodb delete-table --table-name nexus-dev-edgm-cdc-offsets
terraform apply -target=aws_dynamodb_table.cdc_offsets

# 4. Scale up
aws ecs update-service --cluster nexus-dev-edgm --service nexus-dev-edgm-cdc --desired-count 1

# 5. Redeploy pipelines
cd nexus-medallion && databricks bundle deploy -t dev
```

### Inspeccionar DLQ

```bash
aws s3 ls s3://nexus-dev-edgm-cdc/dlq/ --recursive
aws s3 cp s3://nexus-dev-edgm-cdc/dlq/<path>/evt_XXX.json -
```

### Verificar lag end-to-end

```sql
-- Latencia desde Mongo wallTime hasta que el batch llega a bronze
SELECT
  collection_name,
  MAX(__source_ts_ms) AS latest_event_ts,
  (unix_millis(current_timestamp()) - MAX(__source_ts_ms)) / 1000 AS lag_seconds
FROM (
  SELECT 'expenses' AS collection_name, __source_ts_ms FROM nexus_dev.bronze.mongodb_cdc_expenses
  UNION ALL SELECT 'receipts', __source_ts_ms FROM nexus_dev.bronze.mongodb_cdc_receipts
  UNION ALL SELECT 'hitl_tasks', __source_ts_ms FROM nexus_dev.bronze.mongodb_cdc_hitl_tasks
  UNION ALL SELECT 'ocr_extractions', __source_ts_ms FROM nexus_dev.bronze.mongodb_cdc_ocr_extractions
  UNION ALL SELECT 'expense_events', __source_ts_ms FROM nexus_dev.bronze.mongodb_cdc_expense_events
)
GROUP BY collection_name;
```

## Path a prod (Phase B')

Cuando el volumen > 100 msg/s o haya requerimiento de multi-consumer:

1. Reemplazar `main.py` por Debezium Server container (mismo envelope Avro/JSON).
2. Añadir MSK Serverless + Kafka sink.
3. Databricks conector Kafka nativo en lugar de Autoloader S3.

Bronze/silver/gold NO cambian — mismo schema, mismos `__op`/`__source_ts_ms`.
