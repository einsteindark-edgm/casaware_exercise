# Nexus — AWS infra (Terraform)

Declarative AWS resources that back the Nexus stack when `FAKE_PROVIDERS=false`. Currently creates:

| Resource | Purpose |
|---|---|
| S3 bucket `nexus-dev-edgm-receipts` | Stores uploaded receipt PDFs/images that Textract reads |
| IAM user `nexus-dev-edgm-worker` + access key | Used by the backend + worker containers running on your Mac |
| IAM OIDC provider for `token.actions.githubusercontent.com` | Lets GitHub Actions authenticate to AWS without long-lived keys |
| IAM role `nexus-dev-edgm-gha-terraform` | The role GitHub Actions assumes to run Terraform (when we move plan/apply to CI) |

State is **local** (`infra/terraform/terraform.tfstate`). Only apply from your Mac for now. Migrate to a remote S3 backend once a second human needs to apply.

---

## Bootstrap (one-time, ~10 minutes)

### 1. Install the CLIs

```bash
brew install terraform awscli
terraform version   # expect 1.9.x
aws --version       # expect 2.x
```

### 2. Create a bootstrap IAM admin user in the AWS console

Terraform itself needs credentials to create resources. Since the account is brand-new, you do this step manually once.

1. Open the AWS console → **IAM → Users → Create user**.
2. Name: `bootstrap-admin`. Check *Provide user access to the AWS Management Console* if you want console access, otherwise uncheck it.
3. Permissions → **Attach policies directly → AdministratorAccess**. Create user.
4. Open the user → **Security credentials → Access keys → Create access key → Other**.
5. Copy the *Access key ID* + *Secret access key*. You'll paste them in the next step.

### 3. Configure the AWS CLI locally

```bash
aws configure
# AWS Access Key ID    : <from step 2>
# AWS Secret Access Key: <from step 2>
# Default region name  : us-east-1
# Default output format: json

aws sts get-caller-identity
# Should print your account id + the bootstrap-admin ARN.
```

### 4. Apply the Terraform

```bash
cd infra/terraform
terraform init
terraform plan            # review what will be created
terraform apply           # type "yes" when prompted
```

Expect ~8 resources created.

### 5. Read the secrets Terraform generated

```bash
terraform output -raw worker_access_key_id
terraform output -raw worker_secret_access_key
terraform output -raw receipts_bucket
terraform output -raw gha_terraform_role_arn
terraform output -raw aws_account_id
```

These go into two `.env` files (see **Wiring up the apps** below).

---

## Wiring up the apps

### Backend (`backend/.env`)

Create the file if it doesn't exist. It is loaded automatically by both `docker compose` and `uvicorn`.

```bash
# Real AWS
AWS_ACCESS_KEY_ID=<worker_access_key_id>
AWS_SECRET_ACCESS_KEY=<worker_secret_access_key>
AWS_REGION=us-east-1
S3_RECEIPTS_BUCKET=<receipts_bucket>
S3_TEXTRACT_OUTPUT_BUCKET=<receipts_bucket>
# Unset LocalStack — empty string makes boto3 fall back to the real endpoint.
AWS_ENDPOINT_URL=
```

Then **edit `backend/docker-compose.dev.yml`** to remove the hardcoded overrides. In the `backend:` service, delete or comment these four lines:

```yaml
      - S3_RECEIPTS_BUCKET=nexus-receipts-dev
      - S3_TEXTRACT_OUTPUT_BUCKET=nexus-textract-output-dev
      - AWS_ENDPOINT_URL=http://localstack:4566
      - AWS_ACCESS_KEY_ID=test
      - AWS_SECRET_ACCESS_KEY=test
```

and add:

```yaml
    env_file:
      - .env
```

### Worker (`nexus-orchestration/.env`)

```bash
AWS_ACCESS_KEY_ID=<worker_access_key_id>
AWS_SECRET_ACCESS_KEY=<worker_secret_access_key>
AWS_REGION=us-east-1
S3_RECEIPTS_BUCKET=<receipts_bucket>
S3_TEXTRACT_OUTPUT_BUCKET=<receipts_bucket>
AWS_ENDPOINT_URL=
FAKE_PROVIDERS=false
# Textract runs for real — no need to force the fake HITL mode anymore.
FAKE_HITL_MODE=auto
```

Edit `nexus-orchestration/docker-compose.workers.yml` similarly — remove the hardcoded `AWS_*`, `S3_*`, `FAKE_PROVIDERS`, `FAKE_HITL_MODE` entries from `x-worker-base.environment` and add `env_file: .env` on each worker service (or, equivalently, run `docker compose --env-file .env -f docker-compose.workers.yml up`).

### Restart the stack

```bash
cd backend
docker compose -f docker-compose.dev.yml up -d

cd ../nexus-orchestration
docker compose -f docker-compose.workers.yml up -d --build
```

Upload a receipt through the frontend. Tail `worker-ocr` — you should see `textract.analyze_expense.response` instead of `fake.textract.*`.

---

## GitHub Actions OIDC

`.github/workflows/terraform.yml` is set up to run `terraform fmt -check` + `terraform validate` on every PR. It **does not** run `plan` or `apply` today because the state is local — CI has no way to see the current state.

When you're ready to promote plan/apply to CI:

1. Create an S3 bucket + DynamoDB table for remote state (can be done in a separate `infra/terraform/bootstrap/` TF root, or manually).
2. Add a `backend "s3" { ... }` block in `versions.tf`.
3. `terraform init -migrate-state` locally — this copies the current state to S3.
4. In the workflow, before `terraform init`, add:

   ```yaml
   - uses: aws-actions/configure-aws-credentials@v4
     with:
       role-to-assume: ${{ secrets.AWS_GHA_ROLE_ARN }}
       aws-region: us-east-1
   ```

5. Set repo secret `AWS_GHA_ROLE_ARN` to the value of `terraform output -raw gha_terraform_role_arn`.
6. Uncomment / add the plan and apply steps per the comment at the bottom of `terraform.yml`.

---

## Destroying everything

```bash
cd infra/terraform
terraform destroy
```

`force_destroy = true` is set on the bucket so it removes even with objects inside. **Nothing warns you if you forgot to download important receipts** — destroy only when you're sure.

---

## Costs

- S3: pennies/month for <100 MB of dev receipts.
- Textract `AnalyzeExpense`: $0.01 per page. 100 receipts ≈ $1.
- IAM / OIDC provider: free.

Budget alert recommended: AWS Console → **Billing → Budgets → Create budget → $5/month** with email alert.
