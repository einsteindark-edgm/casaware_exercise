# Phase B+ · Runbook de cutover

> **Status**: Para ejecutar.
> **Alcance**: `nexus_dev` únicamente. Prod (`nexus_prod`) se replica en un PR posterior tras 2 semanas de observación.
> **Modo**: Clean-cut, sin double-write. `nexus-cdc` (Phase B) se apaga antes de encender Debezium. Una sola arquitectura a la vez, sin confusión sobre quién transporta los datos.

---

## Pre-requisitos

- Merge del PR de Phase B+ (Terraform MSK/Debezium/Databricks_msk + 5 notebooks bronze Kafka + bundles actualizados) en `main`.
- Acceso IAM a AWS (terraform + aws cli + ECR push si fuera necesario).
- Acceso a Databricks workspace con permisos sobre `nexus_dev` catalog.
- `mongosh` apuntando a MongoDB Atlas con rol `dbAdmin` sobre `nexus_dev`.
- `kcat` o `kafka-cli` disponible localmente (para pre-crear topics y smoke-test).

---

## Fase 1 · Infra base (día 1)

### 1.1 Activar MSK en Terraform

```bash
cd infra/terraform
# En terraform.tfvars ya hay msk_enabled=false, debezium_desired_count=0.
# Editarlo:
#   msk_enabled            = true
#   debezium_desired_count = 0   # sigue apagado hasta Fase 2
terraform plan
terraform apply
```

**Esperado**: MSK Serverless tarda ~15 min en crearse. Al final:

```bash
terraform output msk_bootstrap_servers
# -> "boot-xxxxxxxx.c1.kafka-serverless.us-east-1.amazonaws.com:9098"
terraform output databricks_msk_service_credential
# -> "nexus-dev-edgm-msk-cred"
```

### 1.2 Self-assume del role Databricks → MSK

Mismo patrón que `uc_access` en Phase C (ver `databricks.tf:85-93`). El role lo necesita para refrescar credentials desde UC.

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ROLE_ARN="arn:aws:iam::$ACCOUNT_ID:role/nexus-dev-edgm-msk-databricks"

aws iam update-assume-role-policy \
  --role-name nexus-dev-edgm-msk-databricks \
  --policy-document "$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DatabricksAssume",
      "Effect": "Allow",
      "Principal": { "AWS": "arn:aws:iam::414351767826:role/unity-catalog-prod-UCMasterRole-14S5ZJVKOTYTL" },
      "Action": "sts:AssumeRole",
      "Condition": { "StringEquals": { "sts:ExternalId": "a36b2e73-dd25-474f-9b8d-aacca9a59402" } }
    },
    {
      "Sid": "SelfAssume",
      "Effect": "Allow",
      "Principal": { "AWS": "$ROLE_ARN" },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF
)"
```

### 1.3 Pre-crear topics

MSK Serverless auto-creation puede ser opaca. Preferimos crear los topics explícitos con retention/compression deseados. Desde una instancia con acceso al VPC (o EC2 temporal):

```bash
export MSK=$(terraform output -raw msk_bootstrap_servers)

# Requiere kafka-cli con aws-msk-iam-auth. Ejemplo con AWS CloudShell:
cat > client.properties <<EOF
security.protocol=SASL_SSL
sasl.mechanism=AWS_MSK_IAM
sasl.jaas.config=software.amazon.msk.auth.iam.IAMLoginModule required;
sasl.client.callback.handler.class=software.amazon.msk.auth.iam.IAMClientCallbackHandler
EOF

for t in expenses receipts hitl_tasks ocr_extractions expense_events; do
  kafka-topics.sh --bootstrap-server $MSK \
    --command-config client.properties \
    --create --topic nexus.nexus_dev.$t \
    --partitions 6 --replication-factor 3 \
    --config retention.ms=604800000 \
    --config compression.type=zstd
done

# DLQ separado (más chico, retention corto para inspección manual).
kafka-topics.sh --bootstrap-server $MSK \
  --command-config client.properties \
  --create --topic nexus.dlq \
  --partitions 3 --replication-factor 3 \
  --config retention.ms=259200000
```

### 1.4 Habilitar pre-images en Mongo Atlas

Requiere MongoDB 6.0+. Idempotente: si ya está habilitado, no rompe.

```javascript
// mongosh "mongodb+srv://..."
use nexus_dev

for (const coll of ["expenses", "receipts", "hitl_tasks", "ocr_extractions", "expense_events"]) {
  db.runCommand({
    collMod: coll,
    changeStreamPreAndPostImages: { enabled: true }
  });
}
```

**Nota**: bloquea escrituras ~10-30s por colección en volumen grande. En `nexus_dev` con <100 docs por coll es instantáneo.

---

## Fase 2 · Arrancar Debezium y validar (día 2)

### 2.1 Subir task count de Debezium

```bash
# En terraform.tfvars:
#   debezium_desired_count = 1
terraform apply
```

ECS arranca la task. CloudWatch logs: `/ecs/nexus-dev-edgm/debezium`. Esperado en los primeros ~2 min:

- Conexión a Mongo OK (`MongoDbConnector.start`).
- Snapshot de las 5 colecciones (fase `INITIAL`).
- Transición a streaming (`ChangeStream cursor opened`).

### 2.2 Smoke-test del topic

```bash
# Desde CloudShell con client.properties (ver 1.3):
kafka-console-consumer.sh --bootstrap-server $MSK \
  --consumer.config client.properties \
  --topic nexus.nexus_dev.expenses \
  --from-beginning --max-messages 3
```

**Esperado**: JSON con campos del expense + `__op` ("r" durante snapshot, "c/u/d" después) + `__source_ts_ms` + `__deleted`. Si sale JSON con campos BSON `$numberDecimal` o similares, hay que ajustar Debezium con `converter.schemas.enable=false` (ya seteado en debezium.tf, verificar que llegó).

### 2.3 Provocar un cambio y verificar

```javascript
// mongosh
use nexus_dev
db.expenses.insertOne({
  expense_id: "smoke-phase-b-plus",
  tenant_id: "t1",
  amount: 42.0,
  status: "pending",
  created_at: new Date()
});
```

En <5s debe aparecer en el consumer del 2.2 con `"__op":"c"`.

---

## Fase 3 · Cutover del pipeline bronze (día 3)

> **Ventana estimada**: 30 min. Es el único paso con downtime real sobre bronze.

### 3.1 Apagar Phase B

```bash
# En terraform.tfvars, bajar cdc a 0:
#   cdc_desired_count = 0
terraform apply
# ECS drena la task nexus-cdc en ~30s.
```

### 3.2 Parar el pipeline Autoloader existente

```bash
databricks pipelines stop --pipeline-id <id-de-bronze_cdc_pipeline>
# O desde la UI: Workflows → Delta Live Tables → bronze_cdc → Stop.
```

### 3.3 DROP bronze tables

```sql
-- Databricks SQL editor, catalog nexus_dev.
DROP TABLE IF EXISTS nexus_dev.bronze.mongodb_cdc_expenses;
DROP TABLE IF EXISTS nexus_dev.bronze.mongodb_cdc_receipts;
DROP TABLE IF EXISTS nexus_dev.bronze.mongodb_cdc_hitl_tasks;
DROP TABLE IF EXISTS nexus_dev.bronze.mongodb_cdc_ocr_extractions;
DROP TABLE IF EXISTS nexus_dev.bronze.mongodb_cdc_expense_events;
```

**Por qué**: el esquema de las tablas cambia solo en el `comment`, pero DLT valida metadata de checkpoint contra path de origen. Empezar limpio evita estados colgados de Autoloader.

### 3.4 Reset de Debezium (re-snapshot desde cero)

El `FileOffsetBackingStore` vive en `/tmp/offsets.dat` dentro del task — al forzar redeploy, el nuevo task arranca sin offset → `snapshot.mode=initial` re-emite todo.

```bash
aws ecs update-service \
  --cluster nexus-dev-edgm \
  --service nexus-dev-edgm-debezium \
  --force-new-deployment
```

Esperar ~1 min a que el task nuevo termine el snapshot y vuelva a streaming. Ver logs para confirmar.

### 3.5 Poner msk_bootstrap_servers en databricks.yml (target dev)

```yaml
# nexus-medallion/databricks.yml (target dev)
variables:
  msk_bootstrap_servers: "boot-xxxxxxxx.c1.kafka-serverless.us-east-1.amazonaws.com:9098"
```

Commit + push.

### 3.6 Deploy el bundle actualizado

```bash
cd nexus-medallion
databricks bundle deploy --target dev
```

Esto empuja los 5 notebooks nuevos + el pipeline `continuous: true` + el job `cdc_refresh` sin la task bronze_cdc.

### 3.7 Arrancar el pipeline con full-refresh

```bash
databricks pipelines start --pipeline-id <id-de-bronze_cdc_pipeline> --full-refresh-all
```

El pipeline se conecta a MSK desde `earliest`, consume el snapshot + eventos posteriores, crea las 5 tablas bronze desde cero. En dev con <500 eventos totales, <2 min en stabilizarse.

### 3.8 Full-refresh de silver y gold

Los checkpoints previos de silver/gold apuntan a las tablas dropeadas. Hay que resetearlos:

```bash
databricks pipelines start --pipeline-id <silver_pipeline_id> --full-refresh-all
databricks pipelines start --pipeline-id <gold_pipeline_id>   --full-refresh-all
```

---

## Fase 4 · Validación post-cutover

### 4.1 Lag Mongo → bronze

```sql
SELECT
  MAX(__source_ts_ms) AS last_mongo_ts_ms,
  (unix_millis(current_timestamp()) - MAX(__source_ts_ms)) / 1000.0 AS lag_seconds
FROM nexus_dev.bronze.mongodb_cdc_expenses;
-- Esperado en horario activo: lag_seconds < 10.
```

### 4.2 Distribución de `__op`

```sql
SELECT __op, COUNT(*) AS n FROM nexus_dev.bronze.mongodb_cdc_expenses GROUP BY __op;
-- Esperado: r > 0 (snapshot inicial), c > 0 (inserts post-snapshot).
-- Si aparecen __op NULL, el SMT add.fields no se aplicó correctamente.
```

### 4.3 Row count coherencia vs Mongo

```javascript
// mongosh
db.expenses.countDocuments();
// vs
```

```sql
SELECT COUNT(DISTINCT expense_id) FROM nexus_dev.silver.expenses;
-- Deben coincidir (±inserts en vuelo durante la query).
```

### 4.4 Test end-to-end en vivo

```javascript
// mongosh
db.expenses.insertOne({
  expense_id: "e2e-" + new Date().toISOString(),
  tenant_id: "t1",
  amount: 100.5,
  status: "pending",
  created_at: new Date()
});
```

En bronze: <10s.
En silver: próximo trigger (15 min — job `cdc_refresh`).

---

## Fase 5 · Destruir Phase B en AWS (día 5-7, tras 72h sin incidentes)

> **Nota**: los archivos `cdc.tf` y el directorio `nexus-cdc/` **ya fueron eliminados del repo** en este PR (decisión del usuario: "una sola arquitectura real, sin confusión"). Este paso solo destruye las resources en AWS. Hasta que se aplique, los recursos viven "huérfanos" en el state — no se rompe nada, pero conviene ejecutarlo pronto para ahorrar el baseline de ECR/S3/DDB.

```bash
cd infra/terraform
terraform plan -var msk_enabled=true
# Debe mostrar destroy de: aws_ecr_repository.cdc, aws_s3_bucket.cdc + 3 configs,
# aws_dynamodb_table.cdc_offsets, aws_iam_role.cdc_task + policy,
# aws_cloudwatch_log_group.cdc, aws_ecs_task_definition.cdc,
# aws_ecs_service.cdc, aws_ssm_parameter.{cdc_bucket,cdc_offsets_table}.
# Total esperado: 17 destroy, 15 create (MSK+Debezium+UC cred).
terraform apply -var msk_enabled=true
```

Ya no hay referencias a nexus-cdc en `.github/workflows/` (verificado con `grep -r "nexus-cdc" .github/`).

---

## Rollback

Si algo se rompe catastróficamente en las primeras 48h post-cutover (bronze vacío, silver inconsistente, Debezium loopea crashes):

1. `git revert` del PR de Phase B+ (vuelven `cdc.tf`, notebooks Autoloader, `cdc_refresh.yml` con timer).
2. `terraform apply` restaura el stack Phase B (bucket CDC + DDB recreadas vacías, ECR sigue ahí si no lo borraste en Fase 5).
3. `terraform apply -var=cdc_desired_count=1` arranca `nexus-cdc`. Al no haber offset en DDB, hace bootstrap full-sync → emite todos los docs como `__op=r`.
4. `databricks pipelines start bronze_cdc_pipeline --full-refresh-all` con el notebook Autoloader.
5. Silver + gold con `--full-refresh-all`.

**Costo del rollback**: ~1h trabajo + re-snapshot completo. Aceptable en dev.

---

## Notas de seguridad

- `nexus-dev-edgm-msk-sg` deja `0.0.0.0/0:9098` ingress (IAM-gated). En prod cerrar a los CIDRs del plano de control Databricks us-east-1.
- `FileOffsetBackingStore` de Debezium pierde offsets en restart. Antes de prod migrar a `KafkaOffsetBackingStore` con topic interno `_debezium_offsets`.
- El Service Credential `nexus-dev-edgm-msk-cred` da permisos `kafka-cluster:ReadData` sobre TODOS los topics del cluster. Si aparece un segundo pipeline que no debe leer bronze events, crear un Service Credential nuevo con ARN de topic más específico.
