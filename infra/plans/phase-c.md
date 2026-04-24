# Phase C — Medallion en Databricks (Unity Catalog + Vector Search) sobre AWS

> **Objetivo:** tener un lakehouse Delta gobernado (bronze/silver/gold) con Unity Catalog, más un índice de Mosaic AI Vector Search poblado de chunks reales, alimentando al chatbot RAG del worker Temporal. Al final, el worker puede operar con `FAKE_PROVIDERS=false` para Vector Search y devolver citations de datos reales.
>
> **Por qué primero C y no B (CDC):** la capa Databricks se puede construir ya mismo con un *seed* sintético desde MongoDB Atlas — no necesita CDC. En cambio, CDC sin Databricks deja Parquet en S3 sin consumidor. Hacemos C primero para tener algo demostrable; Phase B reemplazará el seed con ingestion en streaming después.
>
> **No incluye:** Debezium/Kafka (Phase B), OCR drift monitoring (dependiente de datos reales de Textract), CI/CD de bundles (lo añadimos cuando los pipelines estén estables).

---

## 1 · Arquitectura objetivo

```
┌────────────────────────────────────────────────────────────┐
│                    Databricks Workspace                     │
│                   (AWS us-east-1, Premium)                  │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐  │
│  │             Unity Catalog · nexus_dev                 │  │
│  │                                                       │  │
│  │  bronze ──────► silver ──────► gold ──────► vector   │  │
│  │                                                       │  │
│  │  ┌────────┐   ┌────────┐   ┌────────────┐            │  │
│  │  │ mongodb│   │ expenses│   │ expense_   │            │  │
│  │  │ _cdc_* │──►│         │──►│   audit    │            │  │
│  │  │ seed   │   │ ocr_*   │   │ expense_   │            │  │
│  │  │ tables │   │         │   │   chunks   │            │  │
│  │  └────────┘   └────────┘   └──────┬─────┘            │  │
│  │                                   │                   │  │
│  │                          ┌────────▼──────────┐        │  │
│  │                          │ Delta Sync Index  │        │  │
│  │                          │ expense_chunks_ix │        │  │
│  │                          └────────┬──────────┘        │  │
│  └───────────────────────────────────┼───────────────────┘  │
│                                      │                      │
│                       ┌──────────────▼─────────────────┐    │
│                       │  Vector Search Endpoint        │    │
│                       │  (STANDARD, bge-large-en)      │    │
│                       └──────────────┬─────────────────┘    │
└──────────────────────────────────────┼──────────────────────┘
                                       │ HTTPS
                                       │
                         ┌─────────────▼───────────────┐
                         │ ECS Fargate · worker-rag    │
                         │ (AWS, VPC privado)          │
                         │                             │
                         │ vector_similarity_search    │
                         │ activity → Databricks VS    │
                         └─────────────────────────────┘

External Location (S3):
  s3://nexus-dev-edgm-uc/          ← managed por Unity Catalog
  s3://nexus-dev-edgm-receipts/    ← ya existe (phase A)
  s3://nexus-dev-edgm-textract-*/  ← ya existe (phase A)
```

**Decisiones clave:**

- **Workspace creado manualmente en UI (no Terraform)**. Razón: crear un workspace con Terraform requiere cross-account IAM con 8 recursos y ~300 líneas de HCL. La UI lo hace en 5 minutos con "Quick Start (automated)". El valor de IaC está en UC + Bundles, no en el workspace en sí.
- **Unity Catalog 100% vía Terraform** (`databricks/databricks` provider). Catalog, schemas, external locations, grants, row filters — todo versionado.
- **Asset Bundle (`databricks.yml`)** para pipelines Lakeflow + Jobs. Despliega con `databricks bundle deploy --target dev`.
- **Seed desde MongoDB Atlas** en lugar de Auto Loader sobre S3. Razón: Phase A ya tiene datos reales en Mongo; seed en una sola pasada = bronze poblado en 30s. Phase B reemplazará esto con CDC en streaming.
- **Vector Search STANDARD, no STORAGE_OPTIMIZED**. Razón: <1M chunks esperados en dev, STANDARD escala a 0 cuando no hay queries = prácticamente gratis fuera de trial.
- **Embedding: `databricks-bge-large-en`** (Foundation Model API, pay-per-token). 1024 dim, bueno en español, no requiere self-hosting.
- **Worker queda en modo híbrido**: `FAKE_PROVIDERS=true` para Textract+Bedrock (no hay créditos), `FAKE_PROVIDERS=false` solo para Vector Search. Se logra con una env var adicional `FAKE_VECTOR_SEARCH=false` evaluada dentro del activity.

---

## 2 · Costo estimado (dev, 24/7, sin uso pesado)

| Recurso | Unidad | $/mes (trial) | $/mes (post-trial) |
|---|---|---|---|
| Databricks workspace (Premium) | account | $0 (14-day free trial) | mínimo $0 base, pago por DBU |
| Lakeflow pipelines | 2 × continuous serverless | incluido en trial | ~$15/día si continuous 24/7 → **apagar después de cada demo** |
| SQL Warehouse serverless | solo para queries puntuales | $0 si no se usa | $0.70/h auto-stop |
| Vector Search STANDARD endpoint | scales-to-zero | $0 hasta QPS >0 | ~$2/h cuando activo; $0/h dormido |
| Foundation Model API (embeddings) | pay-per-token | trial | ~$0.10 por 1M tokens |
| S3 UC root bucket | <1 GB datos dev | $0.03 | $0.03 |
| **Total dev (solo durante demos)** | | ~$0 (trial) | ~$20–40 si apagás pipelines |

**Estrategia de costos:** pipelines Lakeflow en modo **triggered** (no continuous) durante dev — corren cuando los invocás, no 24/7. Vector Search endpoint se pausa automático tras 10 min sin queries. Workspace idle = ~$0.03/día (solo el bucket UC).

---

## 3 · Prerrequisitos

- Phase A desplegada (ECS + MongoDB Atlas + Redis + Cognito funcionando).
- Datos en Mongo Atlas: al menos 5 expenses con OCR extractions (los que ya generaste testeando HITL).
- Cuenta AWS con rol de admin (ya existe).
- Email personal para signup de Databricks (gratis, 14 días trial Premium).
- Databricks CLI v0.220+ instalado localmente (`brew install databricks/tap/databricks`).

---

## 4 · Subfases

### C.0 — Signup + workspace + metastore *(manual, ~10 min)*

Este paso es manual porque crear un workspace vía Terraform requiere credenciales a nivel de cuenta (`DATABRICKS_ACCOUNT_ID` + service principal) que no tienen sentido para un solo workspace de dev. Seguí el runbook en `§9.0 Runbook C.0`.

**Entregables de este paso** (el usuario los pega en `infra/terraform/terraform.tfvars` al final):
- `databricks_host` (ej: `https://dbc-abcde-123.cloud.databricks.com`).
- `databricks_account_id` (ej: `abcd1234-...`).
- `databricks_pat_token` (personal access token del workspace).
- `databricks_metastore_id` (Unity Catalog metastore ID, se lee en la UI al abrir Catalog Explorer).

### C.1 — Scaffolding del proyecto `nexus-medallion/` *(Claude)*

Estructura completa del Asset Bundle: `databricks.yml`, `resources/`, `src/{seed,silver,gold,vector,governance}/`, `tests/`, `.gitignore`.

Criterio: `cd nexus-medallion && databricks bundle validate --target dev` pasa sin errores (requiere las vars de C.0).

### C.2 — Terraform Databricks provider *(Claude)*

- `infra/terraform/databricks.tf`: provider, catalog, schemas, external location, grants base.
- `infra/terraform/s3.tf` (extender): bucket `nexus-dev-edgm-uc` con policy para UC storage credential.
- `infra/terraform/variables.tf`: vars nuevas (`databricks_host`, `databricks_token`, `databricks_metastore_id`).
- `infra/terraform/outputs.tf`: `databricks_catalog`, `databricks_uc_bucket`.

Criterio: `terraform apply -target=databricks_catalog.nexus_dev` crea el catálogo y los 5 schemas.

### C.3 — Seed bronze desde Mongo Atlas *(Claude)*

- `src/seed/seed_bronze_from_mongo.py`: notebook Python que lee 5 colecciones (expenses, receipts, hitl_tasks, ocr_extractions, expense_events) y las escribe a 5 tablas bronze con el schema esperado por `apply_changes`.
- Agrega columnas `__op='c'`, `__source_ts_ms` (epoch ms de `updated_at` o `created_at`), `__deleted=false`.
- `resources/jobs/seed_bronze.yml`: job de ejecución puntual (se corre una vez, luego Phase B lo reemplaza con CDC streaming).

Criterio: tras correr el job, `SELECT COUNT(*) FROM nexus_dev.bronze.mongodb_cdc_expenses` devuelve N = # expenses en Mongo.

### C.4 — Silver pipeline (Lakeflow) *(Claude)*

- `src/silver/{expenses,ocr_extractions,expense_events,hitl_events}.py` con `@dlt.view` + `dlt.apply_changes(stored_as_scd_type="1")`.
- `resources/pipelines/silver.yml`: Lakeflow pipeline, **triggered** en dev (cost control).
- `src/silver/expenses.py` extrae `final_amount`, `final_vendor`, `final_date`, `source_per_field`, `approved_at` directamente del doc Mongo (los embed el worker al aprobar, ya están ahí en la colección `expenses`).

Criterio: tras `databricks bundle run silver_pipeline`, `SELECT status, COUNT(*) FROM nexus_dev.silver.expenses GROUP BY status` retorna la foto viva.

### C.5 — Gold tables (Lakeflow) *(Claude)*

- `src/gold/expense_audit.py`: materializa solo las filas con `status='approved'` de silver.expenses, con SCD1 por `expense_id`.
- `src/gold/expense_chunks.py`: `@dlt.table` genera un chunk por expense aprobado con texto en español: `"Gasto en {vendor} por {amount} {currency} el {date}. Categoría: {category}."`.
- Agrega `ALTER TABLE ... SET TBLPROPERTIES (delta.enableChangeDataFeed = true)` al final (requerido por Vector Search Delta Sync).
- `resources/pipelines/gold.yml`.

Criterio: `SELECT chunk_id, chunk_text FROM nexus_dev.gold.expense_chunks LIMIT 3` muestra filas legibles.

### C.6 — Vector Search *(Claude)*

- `src/vector/setup_vector_search.py`: crea endpoint STANDARD `nexus-vs-dev` y Delta Sync index `nexus_dev.vector.expense_chunks_index` sobre gold.expense_chunks.
- Include `tenant_id` en `columns_to_sync` (crítico para filtro multi-tenant).
- `resources/jobs/vector_setup.yml`: one-shot job.

Criterio: tras correr el job, `client.get_index(...).similarity_search(query_text="café", filters='tenant_id="t_alpha"', num_results=3)` desde notebook devuelve chunks.

### C.7 — Worker usa Vector Search real *(Claude + user)*

- `infra/terraform/secrets.tf` extender: 3 nuevos secretos (`databricks_host`, `databricks_token`, `databricks_vs_index`).
- `infra/terraform/ecs_workers.tf`: añadir 4 env vars (host, token via secret, endpoint, index) + variable `FAKE_VECTOR_SEARCH=false`.
- `nexus-orchestration/src/nexus_orchestration/activities/vector_search.py`: modificar el `if settings.fake_providers` a `if settings.fake_vector_search`.
- `nexus-orchestration/src/nexus_orchestration/config.py`: añadir `fake_vector_search: bool = True` (default para que dev local no cambie).
- `nexus-orchestration/pyproject.toml`: `databricks-vectorsearch` dep en extras `[prod]` → moverla a runtime.
- Build + push imagen worker v2 a ECR.
- Force redeploy del service worker.
- **Verificar E2E:** `POST /chat` con mensaje `"¿cuánto gasté en Starbucks?"` → SSE devuelve `chat.complete` con citations donde `source_url` apunta a expenses reales.

Criterio: en el frontend, el panel de chat muestra una respuesta con ≥1 citation linkeada a una factura aprobada real (no sintética).

---

## 5 · Orden de ejecución (checklist)

```
[x] C.0 Signup + workspace + metastore       (manual, usuario)
[ ] C.1 nexus-medallion/ scaffolding          (claude)
[ ] C.2 Terraform databricks provider         (claude)
[ ] C.3 Seed bronze desde Mongo               (claude)
[ ] C.4 Silver DLT pipeline                   (claude)
[ ] C.5 Gold tables                           (claude)
[ ] C.6 Vector Search endpoint + index        (claude)
[ ] C.7 Worker con VS real                    (claude + push)
```

El usuario solo tiene que hacer C.0 manualmente y ejecutar 2 comandos (`terraform apply` + `databricks bundle deploy`) en C.2/C.4.

---

## 6 · Criterios de aceptación end-to-end

1. `SHOW CATALOGS` desde el SQL Editor de Databricks muestra `nexus_dev` con owner `<tu usuario>`.
2. `DESCRIBE SCHEMA nexus_dev.silver` lista 5 tablas: expenses, receipts, ocr_extractions, expense_events, hitl_events.
3. `SELECT COUNT(*) FROM nexus_dev.gold.expense_chunks WHERE tenant_id = 't_alpha'` retorna > 0.
4. Query `similarity_search(query_text="Starbucks", filters='tenant_id="t_alpha"')` devuelve chunks relevantes; la misma query con `tenant_id="t_beta"` NO retorna esos chunks (aislamiento multi-tenant).
5. En el frontend, chat → mensaje → respuesta con citations. Los logs de ECS del worker-rag muestran `vector_similarity_search` completando sin `fake_providers=true`.
6. `terraform destroy -target=databricks_catalog.nexus_dev` es posible sin errores (reversibilidad IaC).

---

## 7 · Riesgos y mitigaciones

| Riesgo | Impacto | Mitigación |
|---|---|---|
| Trial Databricks de 14 días expira | Pipelines dejan de correr | Exportar DDL de tablas + scripts seed. Re-ejecución = 30 min. |
| Costos de pipeline continuous | $15/día escondidos | Usar `continuous: false` en resource yml; invocar con `bundle run`. |
| UC metastore no existe al correr TF | `terraform apply` falla | C.0 runbook explicita: crear metastore **antes** de TF apply. |
| Vector Search sin `tenant_id` en filter | Leak cross-tenant | Test en `tests/integration/test_vs_multitenant.py` que asserta 0 resultados cuando filtro mismatch. |
| `databricks-vectorsearch` dep añade 40 MB al worker | Imagen más gorda | Aceptable; sigue siendo <500 MB total. |
| Secrets Databricks rotan cada 90 días | ECS tasks fallan silenciosamente | Usar PAT con expiración 1 año + alarma CloudWatch sobre workflow failures. |

---

## 8 · Qué queda fuera (Phase D candidatos)

- **Databricks workspace vía Terraform** (account-level + cross-account IAM).
- **Lakehouse Monitoring** sobre OCR drift (requiere datos reales de Textract).
- **Row-Level Security con variable de sesión** (solo tiene sentido con backend consumiendo SQL Warehouse directo).
- **CI/CD bundle deploy** en GitHub Actions.
- **Column masks** para PII (user_email en receipts).
- **`expense_audit_with_history`** vista enriquecida (nice-to-have).
- **Drift del OCR** con Lakehouse Monitoring (Phase D).

---

## 9 · Runbooks

### 9.0 Runbook C.0 — Signup + workspace + metastore

1. **Signup**
   - Ir a https://www.databricks.com/try-databricks → "Start Free Trial" → elegir **AWS** como cloud.
   - Usar el email real; anotar `workspaceUrl` (ej: `https://dbc-12345678-abcd.cloud.databricks.com`).
   - La UI creará el workspace automáticamente (5-10 min). **Mantener la misma región que Phase A: us-east-1**.

2. **Metastore Unity Catalog**
   - Abrir `https://accounts.cloud.databricks.com` (URL account-level, no workspace).
   - Login con el mismo email.
   - Sidebar → "Data" → "Create metastore".
     - Name: `nexus-dev-metastore`
     - Region: `us-east-1`
     - S3 bucket path: `s3://nexus-dev-edgm-uc/metastore/` (lo creamos en C.2; por ahora podés apuntar a cualquiera y luego actualizás).
     - IAM role: **Create new** (Databricks genera el ARN que hay que asumir; pegá ese ARN acá cuando llegues a Terraform).
   - Asignar el metastore al workspace creado en paso 1.

3. **Personal Access Token (PAT)**
   - Dentro del workspace → top-right avatar → "Settings" → "User Settings" → "Developer" → "Access tokens" → "Generate new token".
   - Comment: `terraform-dev`. Lifetime: 365 días.
   - **Copiar el token** (no se puede volver a ver).

4. **Anotar los IDs necesarios** (los pegás en `terraform.tfvars` en C.2):
   ```
   databricks_host         = "https://dbc-12345678-abcd.cloud.databricks.com"
   databricks_pat_token    = "dapi..."
   databricks_metastore_id = "abcd1234-xxxx-xxxx-xxxx-xxxxxxxxxxxx"  (visible en account console → metastores)
   databricks_account_id   = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"  (top-right del account console)
   ```

5. **Instalar Databricks CLI local**
   ```bash
   brew install databricks/tap/databricks
   databricks --version  # debe ser >= 0.220

   # Configurar profile "nexus-dev"
   databricks configure --profile nexus-dev --host <databricks_host>
   # pegar el PAT cuando lo pida
   ```

6. **Verificar**
   ```bash
   databricks workspace list / --profile nexus-dev
   # debería listar / (raíz vacía, OK)
   ```

### 9.1 Runbook C.2 — Apply Terraform

```bash
cd infra/terraform

# Agregar a terraform.tfvars (git-ignored):
cat >> terraform.tfvars <<EOF
databricks_host         = "https://dbc-..."
databricks_pat_token    = "dapi..."
databricks_metastore_id = "abcd1234-..."
databricks_account_id   = "xxxxxxxx-..."
EOF

terraform init -upgrade    # instala el databricks provider
terraform plan -target=aws_s3_bucket.uc_root
terraform apply -target=aws_s3_bucket.uc_root -auto-approve

terraform plan -target=databricks_storage_credential.nexus_uc
terraform apply -target=databricks_storage_credential.nexus_uc -auto-approve

terraform plan -target=databricks_catalog.nexus_dev
terraform apply -target=databricks_catalog.nexus_dev -auto-approve
# crea catalog + 5 schemas en una pasada
```

### 9.2 Runbook C.3-C.6 — Asset Bundle deploy + run

```bash
cd nexus-medallion

databricks bundle validate --target dev --profile nexus-dev
databricks bundle deploy   --target dev --profile nexus-dev

# Ejecuciones one-shot (seed + vector setup)
databricks bundle run seed_bronze   --target dev --profile nexus-dev
databricks bundle run silver_pipeline --target dev --profile nexus-dev
databricks bundle run gold_pipeline   --target dev --profile nexus-dev
databricks bundle run vector_setup    --target dev --profile nexus-dev

# Verificar
databricks sql-queries list --profile nexus-dev
# O abrir UI: https://<workspace>.cloud.databricks.com/sql/editor
# y correr:
#   SELECT * FROM nexus_dev.gold.expense_chunks LIMIT 5;
```

### 9.3 Runbook C.7 — Worker con VS real

```bash
# 1. Secretos nuevos en AWS Secrets Manager (TF los crea vacíos, los poblás vos)
aws secretsmanager put-secret-value \
  --secret-id nexus-dev-edgm-databricks-host \
  --secret-string "https://dbc-..."

aws secretsmanager put-secret-value \
  --secret-id nexus-dev-edgm-databricks-token \
  --secret-string "dapi..."

# 2. Apply TF para actualizar task def
cd infra/terraform
terraform apply -target=aws_ecs_task_definition.worker -target=aws_ecs_service.worker

# 3. Build + push worker v2
cd ../../nexus-orchestration
docker buildx build --platform linux/amd64 \
  -t 123456789.dkr.ecr.us-east-1.amazonaws.com/nexus-dev-edgm-worker:v2 \
  --push .

aws ecs update-service --cluster nexus-dev-edgm --service nexus-dev-edgm-worker \
  --task-definition nexus-dev-edgm-worker --force-new-deployment

# 4. Watch logs
aws logs tail /ecs/nexus-dev-edgm/worker --follow --filter-pattern vector_similarity
```

---

## 10 · Handoff a Phase B

Cuando Phase C esté verde, Phase B (CDC con Debezium) reemplazará el paso C.3 (seed one-shot) por:
- Kafka Connect + MongoDB connector escribiendo a Kafka topics.
- Auto Loader sobre S3 (sink de Kafka Connect) o Spark Structured Streaming directo desde Kafka, poblando las mismas 5 tablas `bronze.mongodb_cdc_*` — con schema y columnas idénticos al seed.
- El seed job queda como fallback de recovery (si el CDC stream se pierde, re-seed manual).

El resto (silver/gold/vector) **no cambia**. Esa es la ventaja del diseño medallion.
