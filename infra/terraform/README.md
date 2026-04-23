# Nexus — AWS infra (Terraform)

Crea los recursos AWS reales que necesitan el backend y los workers cuando `FAKE_PROVIDERS=false`.

**Recursos que se crean:**
- S3 bucket `nexus-dev-edgm-receipts` (guarda los PDFs que suben los usuarios; Textract los lee)
- S3 bucket `nexus-dev-edgm-textract-output` (el worker guarda aquí el JSON crudo de Textract para lineage; objetos expiran a los 30 días)
- IAM user `nexus-dev-edgm-worker` + access key (lo usan backend y worker desde tu Mac)
- OIDC provider de GitHub + rol `nexus-dev-edgm-gha-terraform` (para que GitHub Actions use credenciales temporales)

**State:** local (`terraform.tfstate` en este directorio). Solo tú puedes aplicar. Cuando alguien más necesite aplicar, migramos a S3 remoto (ver sección final).

---

## Qué tienes que hacer — paso a paso

### Paso 1 · Instalar CLIs (una vez)

```bash
brew install terraform awscli
terraform version   # >= 1.6
aws --version       # 2.x
```

### Paso 2 · Crear un usuario admin de bootstrap en la consola AWS (una vez)

Terraform necesita credenciales para crear todo. Como tu cuenta está vacía, este primer usuario lo creas a mano.

1. Entra a **https://console.aws.amazon.com/iam/** (región us-east-1).
2. **Users → Create user**
   - Name: `bootstrap-admin`
   - No marques "Provide user access to the AWS Management Console" (no lo necesitamos).
3. **Permissions options → Attach policies directly → AdministratorAccess → Next → Create user**.
4. Abre el usuario recién creado → **Security credentials → Create access key**.
   - Use case: **Other** → Next → Create.
   - **Copia el Access key ID y el Secret access key.** El Secret solo se muestra una vez.

### Paso 3 · Configurar AWS CLI con esas credenciales

```bash
aws configure
# AWS Access Key ID    : <del paso 2>
# AWS Secret Access Key: <del paso 2>
# Default region name  : us-east-1
# Default output format: json
```

Verifica:
```bash
aws sts get-caller-identity
# Debe imprimir tu account id + el ARN de bootstrap-admin.
```

### Paso 4 · Aplicar Terraform

```bash
cd infra/terraform
terraform init
terraform plan       # revisa el diff (≈ 8 recursos a crear)
terraform apply      # escribe "yes" cuando pregunte
```

Tarda ~30 segundos.

### Paso 5 · Leer los outputs generados

```bash
terraform output -raw receipts_bucket
terraform output -raw textract_output_bucket
terraform output -raw worker_access_key_id
terraform output -raw worker_secret_access_key
terraform output -raw gha_terraform_role_arn
terraform output -raw aws_account_id
```

Guarda esos 6 valores — los necesitas en el paso siguiente.

### Paso 6 · Conectar backend + workers al AWS real

Crea `/Users/edgm/Documents/Projects/caseware/casaware_exercise/backend/.env`:

```
AWS_ACCESS_KEY_ID=<worker_access_key_id>
AWS_SECRET_ACCESS_KEY=<worker_secret_access_key>
AWS_REGION=us-east-1
S3_RECEIPTS_BUCKET=<receipts_bucket>
S3_TEXTRACT_OUTPUT_BUCKET=<textract_output_bucket>
AWS_ENDPOINT_URL=
```

Crea `/Users/edgm/Documents/Projects/caseware/casaware_exercise/nexus-orchestration/.env`:

```
AWS_ACCESS_KEY_ID=<worker_access_key_id>
AWS_SECRET_ACCESS_KEY=<worker_secret_access_key>
AWS_REGION=us-east-1
S3_RECEIPTS_BUCKET=<receipts_bucket>
S3_TEXTRACT_OUTPUT_BUCKET=<textract_output_bucket>
AWS_ENDPOINT_URL=
FAKE_PROVIDERS=false
FAKE_HITL_MODE=auto
```

Luego me avisas y yo actualizo los dos `docker-compose*.yml` para que lean esos `.env` (hoy tienen las credenciales `test/test` hardcoded; hay que quitarlas y cambiar por `env_file: .env`).

### Paso 7 · Levantar el stack contra AWS real

```bash
cd backend
docker compose -f docker-compose.dev.yml up -d

cd ../nexus-orchestration
docker compose -f docker-compose.workers.yml up -d --build
```

Sube un recibo desde el frontend. En los logs de `worker-ocr` deberías ver `textract.analyze_expense.response` (ya no `fake.textract.*`).

---

## Verificación rápida de que funciona

Desde tu Mac, con las credenciales del `worker` (no el bootstrap-admin):

```bash
export AWS_ACCESS_KEY_ID=$(cd infra/terraform && terraform output -raw worker_access_key_id)
export AWS_SECRET_ACCESS_KEY=$(cd infra/terraform && terraform output -raw worker_secret_access_key)
export AWS_REGION=us-east-1

aws s3 ls s3://$(cd infra/terraform && terraform output -raw receipts_bucket)
# (Debe listar el bucket vacío, sin error de permisos.)

echo "hello" > /tmp/test.txt
aws s3 cp /tmp/test.txt s3://$(cd infra/terraform && terraform output -raw receipts_bucket)/test.txt
aws s3 rm   s3://$(cd infra/terraform && terraform output -raw receipts_bucket)/test.txt
```

Si esos 3 comandos funcionan, las credenciales están bien.

---

## Costos esperados

- S3: céntimos al mes.
- Textract `AnalyzeExpense`: **$0.01 por página**. 100 recibos ≈ $1.
- IAM / OIDC: gratis.

**Recomendado:** AWS Console → **Billing → Budgets → Create budget** → $5/mes con alerta por email.

---

## Cuando termines con esto, destruir todo

```bash
cd infra/terraform
terraform destroy
```

`force_destroy = true` está en el bucket, así que borra objetos y bucket de una. **No hay confirmación adicional sobre qué objetos borras** — si subiste recibos reales, respáldalos antes.

Borra también el usuario `bootstrap-admin` desde la consola AWS (Terraform no lo maneja porque lo creaste a mano).

---

## Futuro: mover plan/apply a GitHub Actions

Hoy `.github/workflows/terraform.yml` solo corre `fmt -check` + `validate` porque el state es local. Para promover a CI:

1. Crear un bucket + tabla DynamoDB para state remoto (puede ser otro TF root en `infra/terraform/bootstrap/` o manual).
2. Añadir `backend "s3" { ... }` a `versions.tf`.
3. `terraform init -migrate-state` localmente — copia el state actual a S3.
4. En el workflow, antes de `terraform init`, añadir:

   ```yaml
   - uses: aws-actions/configure-aws-credentials@v4
     with:
       role-to-assume: ${{ secrets.AWS_GHA_ROLE_ARN }}
       aws-region: us-east-1
   ```

5. En GitHub → repo settings → Secrets → añadir `AWS_GHA_ROLE_ARN` con el valor de `terraform output -raw gha_terraform_role_arn`.
6. Añadir steps de `plan` (en PR) y `apply` (en push a main) — hay un comentario al final de `terraform.yml` con el patrón.
