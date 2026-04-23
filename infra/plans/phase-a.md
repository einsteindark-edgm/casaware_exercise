# Phase A — Backend + Frontend + Datastores en AWS

> **Objetivo:** tener una URL pública `https://nexus.<tu-dominio>` (o el DNS del ALB) donde cualquiera pueda iniciar sesión, subir un recibo y ver el flujo end-to-end funcionando, con backend + frontend + MongoDB + Redis + auth real corriendo en AWS. Temporal y sus workers siguen locales, expuestos vía ngrok.
>
> **No incluye:** CDC (Phase B), Databricks (Phase C), Textract real (bloqueado esperando activación de la cuenta — seguimos con `FAKE_PROVIDERS=true` hasta que AWS destrabe).

---

## 1 · Arquitectura objetivo

```
                                ┌─────────────────────────┐
                                │   Internet (usuarios)    │
                                └────────────┬────────────┘
                                             │ HTTPS
                                             ▼
                           ┌─────────────────────────────────┐
                           │ Application Load Balancer (ALB) │
                           │  /*       → frontend TG         │
                           │  /api/*   → backend TG          │
                           │  /events/ → backend TG (SSE)    │
                           └────────┬────────────────┬───────┘
                                    │                │
                        ┌───────────▼───┐  ┌─────────▼──────────┐
                        │ ECS Fargate   │  │  ECS Fargate        │
                        │ frontend      │  │  backend (FastAPI) │
                        │ (Next.js      │  │  uvicorn :8000     │
                        │  SSR :3000)   │  │                    │
                        └───────┬───────┘  └───┬─────────┬──────┘
                                │              │         │
                                └──► Cognito   │         │
                                   (Hosted UI) │         │
                                               │         │
                     ┌─────────────────────────▼─┐  ┌────▼────────────────┐
                     │ MongoDB Atlas M0/M10       │  │ ElastiCache Redis    │
                     │ (fuera de AWS, peering     │  │ t4g.micro            │
                     │  vía public IP whitelisted)│  │ (dentro de VPC)      │
                     └────────────────────────────┘  └──────────────────────┘

                                    ▲
                                    │ gRPC :7233
                                    │ (atravesando ngrok tunnel)
                                    │
                      ┌─────────────┴──────────────┐
                      │  ngrok tunnel              │
                      │  nexus-temporal.ngrok.app  │
                      └─────────────┬──────────────┘
                                    │
                      ┌─────────────▼──────────────┐
                      │ Mac local (tu equipo)      │
                      │  • Temporal dev server     │
                      │  • 4 workers Docker        │
                      │  • LocalStack (solo si     │
                      │     FAKE_PROVIDERS=true)   │
                      └────────────────────────────┘
```

**Decisiones clave:**
- **ECS Fargate** para ambos (backend + frontend). Razón: IaC pura, fácil de demostrar en entrevista. Alternativa considerada: Amplify Hosting para frontend — más rápido pero menos "terraformable".
- **MongoDB Atlas** (no DocumentDB). Razón: Phase B necesita Change Streams compatibles con Debezium; DocumentDB no los soporta bien. Arrancamos en M0 (gratis) y subimos a M10 cuando empecemos Phase B.
- **ElastiCache Redis** vs Upstash: ElastiCache es consistente con el resto del stack AWS y tiene SLA claro. Upstash sería más barato pero introduce otro provider.
- **Cognito User Pool** reemplaza el JWT `dev-secret` del backend. El frontend cambia de login form local a Cognito Hosted UI.
- **Temporal local + ngrok**: evitamos Temporal Cloud ($200/mo) y EKS (caro/complejo). Los workers siguen en tu Mac, el backend en AWS los alcanza vía tunnel. Limitación: si apagas tu Mac, el sistema deja de procesar workflows (aceptable para demo de entrevista).

---

## 2 · Costo estimado (dev, 24/7)

| Recurso | Unidad | $/mes |
|---|---|---|
| ECS Fargate backend | 0.25 vCPU, 0.5 GB × 1 task | 7 |
| ECS Fargate frontend | 0.25 vCPU, 0.5 GB × 1 task | 7 |
| ALB | base + low traffic | 17 |
| NAT Gateway (1 AZ) | base | 32 |
| ElastiCache t4g.micro | | 12 |
| CloudWatch Logs + Metrics | low volume | 2 |
| Secrets Manager | 5 secretos | 2 |
| Route 53 hosted zone | (solo si usas dominio) | 0.50 |
| ACM cert | gratis | 0 |
| MongoDB Atlas M0 | free tier | 0 |
| Cognito | < 50 MAUs | 0 |
| ngrok | plan free | 0 |
| **Total** | | **~$80** |

**Optimizaciones si el costo aprieta:**
- Eliminar NAT Gateway → ECS tasks en subnets *públicas* con IP asignada (menos seguro, ahorra $32/mo).
- Consolidar backend+frontend en un solo container (nginx reverse-proxy) — ahorra ~$7/mo.
- Usar una sola zona de disponibilidad (no HA) para todo — ya asumido en el plan.

**Cuando NO estés demostrando:** `terraform destroy -target=module.ecs` (o similar) apaga los cómputos. ALB + Atlas + Cognito pueden quedar arriba (~$20/mo residual).

---

## 3 · Pre-requisitos (checklist antes de escribir TF)

- [ ] Cuenta AWS activada (estado actual: ✅ Rekognition funciona, Textract pendiente — OK para Phase A).
- [ ] AWS CLI con credenciales `bootstrap-admin` (ya lo tienes).
- [ ] Terraform ≥ 1.6 instalado (ya lo tienes).
- [ ] Cuenta **MongoDB Atlas** creada en `https://cloud.mongodb.com/` (gratis).
- [ ] Cuenta **ngrok** creada en `https://ngrok.com/` (gratis, 1 tunnel concurrente).
- [ ] GitHub repo con actions habilitadas (ya lo tienes).
- [ ] **Opcional:** dominio propio en Route 53 o similar (no imprescindible — podemos usar el DNS del ALB + cert self-signed o simplemente HTTP en demo).

---

## 4 · Orden de implementación (10 fases pequeñas)

Cada paso deja algo verificable antes de pasar al siguiente.

### Fase A.0 · Scaffolding

**Qué:** crear módulos/archivos Terraform nuevos, sin aplicar aún.

**Files nuevos:**
```
infra/terraform/
├── network.tf              # VPC + 2 AZ + IGW + NAT Gateway
├── security_groups.tf      # SGs para ALB, ECS, Redis
├── cognito.tf              # User Pool + App Client + domain
├── ecr.tf                  # 2 repos: nexus-backend, nexus-frontend
├── elasticache.tf          # Redis t4g.micro
├── secrets.tf              # Secrets Manager con Mongo URI, ngrok URL
├── acm.tf                  # (opcional) ACM cert si hay dominio
├── alb.tf                  # ALB + listener 443/80 + target groups
├── ecs_cluster.tf          # Cluster + IAM roles comunes
├── ecs_backend.tf          # Task def + service backend
├── ecs_frontend.tf         # Task def + service frontend
├── cloudwatch.tf           # Log groups
└── outputs.tf              # Extender con ALB DNS, Cognito pool id, ECR urls
```

**Validación:** `terraform fmt -check && terraform validate` → verde. No `apply` todavía.

---

### Fase A.1 · Networking base

**Qué:** VPC + subnets + NAT.

**Recursos TF:**
- `aws_vpc` `10.0.0.0/16` con DNS habilitado.
- 2 subnets públicas `10.0.1.0/24`, `10.0.2.0/24` en us-east-1a/1b.
- 2 subnets privadas `10.0.11.0/24`, `10.0.12.0/24` en us-east-1a/1b.
- Internet Gateway en VPC.
- 1 NAT Gateway + Elastic IP en subnet pública A.
- Route tables: públicas → IGW, privadas → NAT.

**Output:** `vpc_id`, `public_subnet_ids`, `private_subnet_ids`.

**Validación:** `terraform apply`; luego desde la consola VPC ver que hay 4 subnets + 1 NAT.

---

### Fase A.2 · Cognito User Pool

**Qué:** user pool + app client + hosted UI domain.

**Recursos TF:**
- `aws_cognito_user_pool` con self-signup activo, email verification.
- `aws_cognito_user_pool_client` con OAuth flows `code`, scopes `openid profile email`, callback URLs apuntando al ALB (placeholder por ahora, se ajusta después).
- `aws_cognito_user_pool_domain` del tipo `nexus-dev-edgm.auth.us-east-1.amazoncognito.com`.
- Custom attributes: `custom:tenant_id` (el backend lo lee del JWT para RLS).
- Post-confirmation Lambda: **no** en Phase A — asignar `tenant_id` manualmente via `aws cognito-idp admin-update-user-attributes` durante pruebas.

**Outputs:** `cognito_user_pool_id`, `cognito_client_id`, `cognito_jwks_url`, `cognito_domain`.

**Validación:**
```bash
aws cognito-idp sign-up --client-id <id> --username test@test.com --password 'Test123!'
aws cognito-idp admin-confirm-sign-up --user-pool-id <id> --username test@test.com
aws cognito-idp admin-initiate-auth --user-pool-id <id> --client-id <id> \
  --auth-flow ADMIN_NO_SRP_AUTH --auth-parameters USERNAME=test@test.com,PASSWORD='Test123!'
```
→ JWT real devuelto.

---

### Fase A.3 · MongoDB Atlas

**Qué:** cluster M0 + usuario + IP whitelisting.

**Cómo (manual, no-Terraform):** Atlas tiene provider TF pero para un M0 dev es más rápido hacerlo en la UI.

1. `https://cloud.mongodb.com/` → Create Project `nexus-dev`.
2. **Build a Database** → Free tier M0 → AWS → us-east-1 → Cluster name `nexus-dev`.
3. **Database Access** → Add user `nexus_app` con password random, role `readWrite@nexus_dev`.
4. **Network Access** → temporalmente permitir `0.0.0.0/0` (mientras ajustamos al Elastic IP del NAT).
5. Copiar connection string `mongodb+srv://nexus_app:<pass>@nexus-dev.xxx.mongodb.net/?retryWrites=true&w=majority`.

**Cuando sea estable**, restringir Network Access al Elastic IP del NAT Gateway (output de Phase A.1).

**Guardar en Secrets Manager:**
```bash
aws secretsmanager create-secret --name nexus/dev/mongodb_uri \
  --secret-string "mongodb+srv://nexus_app:<pass>@..."
```

**Validación:** `mongosh "<uri>" --eval "db.runCommand({ping:1})"` → `{ok:1}`.

---

### Fase A.4 · ElastiCache Redis

**Qué:** cluster Redis t4g.micro en subnets privadas.

**Recursos TF:**
- `aws_elasticache_subnet_group` con las 2 subnets privadas.
- `aws_security_group` permitiendo 6379 desde el SG de ECS.
- `aws_elasticache_cluster` con engine=`redis`, node_type=`cache.t4g.micro`, num_cache_nodes=1.

**Output:** `redis_endpoint`, `redis_port`.

**Validación:** desde un task ECS temporal (o EC2 jumpbox) `redis-cli -h <endpoint> PING` → `PONG`.

---

### Fase A.5 · ECR + build/push de imágenes

**Qué:** dos repos ECR + GitHub Actions workflow que buildea y pushea.

**Recursos TF:**
- `aws_ecr_repository` `nexus-backend` y `nexus-frontend` con `scan_on_push=true`, `image_tag_mutability=IMMUTABLE`.
- Lifecycle policy: retener solo las 20 imágenes más recientes.

**Nuevo workflow `.github/workflows/build-and-push.yml`:**
- Trigger: push a `main` en `backend/**` o `frontend/**`.
- Steps: checkout → configure-aws-credentials (OIDC role del `nexus-dev-edgm-gha-terraform`) → ECR login → docker build → docker push con tag `git-sha`.
- Por servicio: solo buildea+pushea el que cambió.

**Extender el rol OIDC de GHA (`iam_github_oidc.tf`):** añadir permisos `ecr:PutImage`, `ecr:InitiateLayerUpload`, etc., sobre los nuevos repos.

**Validación:** commit dummy en `backend/README.md` → GHA corre → imagen aparece en ECR.

---

### Fase A.6 · Secrets Manager + Parameter Store

**Qué:** centralizar configuración de runtime.

**Secretos** (cifrado, pago por secreto):
- `nexus/dev/mongodb_uri` (de Phase A.3)
- `nexus/dev/cognito_client_secret` (si el app client lo tiene)
- `nexus/dev/temporal_ngrok_url` (placeholder hasta Phase A.9)
- `nexus/dev/redis_url` (Phase A.4 output)

**SSM Parameters** (plaintext, gratis):
- `/nexus/dev/aws_region`
- `/nexus/dev/s3_receipts_bucket` (ya existe, referenciar)
- `/nexus/dev/cognito_user_pool_id`
- `/nexus/dev/cognito_client_id`

**IAM:** el task role de ECS (siguiente paso) necesita `secretsmanager:GetSecretValue` y `ssm:GetParameter` sobre esos recursos.

**Validación:** `aws secretsmanager list-secrets | grep nexus` → muestra 4 secretos.

---

### Fase A.7 · ALB + ACM (opcional) + DNS

**Qué:** balanceador con 2 target groups y reglas por path.

**Recursos TF:**
- `aws_lb` type=application, en subnets públicas, security group que permite 80/443 desde 0.0.0.0/0.
- `aws_lb_target_group` `nexus-backend-tg` (port 8000, protocol HTTP, health check `/healthz`).
- `aws_lb_target_group` `nexus-frontend-tg` (port 3000, protocol HTTP, health check `/`).
- `aws_lb_listener` en 80 (HTTP) que redirige a 443 (solo si hay cert).
- `aws_lb_listener` en 443 (HTTPS, si hay ACM cert) con default action a frontend TG.
- `aws_lb_listener_rule` priority 10: path `/api/*` y `/healthz` → backend TG.
- `aws_lb_listener_rule` priority 20: path `/api/v1/events/*` → backend TG (sticky sessions para SSE).

**Si hay dominio:**
- `aws_acm_certificate` con DNS validation.
- `aws_route53_record` para `CNAME` validation + `ALIAS` nexus.tu-dominio.com → ALB.

**Si no hay dominio:** usar `<alb-dns>.us-east-1.elb.amazonaws.com`, solo HTTP (insegura para prod, OK para demo).

**Output:** `alb_dns_name`, `alb_zone_id`.

**Validación:** `curl http://<alb-dns>/` → 503 por ahora (no hay targets). Esperado.

---

### Fase A.8 · ECS Cluster + Services

**Qué:** cluster + task definitions + services para backend y frontend.

**Recursos TF:**
- `aws_ecs_cluster` `nexus-dev-edgm`.
- `aws_iam_role` `ecs-task-execution` con `AmazonECSTaskExecutionRolePolicy` + permisos para leer de ECR y escribir logs a CloudWatch.
- `aws_iam_role` `ecs-task-backend` (task role runtime): permisos para `secretsmanager:GetSecretValue`, `ssm:GetParameter`, `s3:GetObject/PutObject` sobre los 2 buckets de receipts/output, `textract:*` (para cuando se active).
- `aws_iam_role` `ecs-task-frontend` (task role): mínimo, solo `secretsmanager:GetSecretValue` para Cognito client secret.
- `aws_cloudwatch_log_group` `/ecs/nexus/backend` y `/ecs/nexus/frontend`.

**Task def backend:**
- Container: `<account>.dkr.ecr.us-east-1.amazonaws.com/nexus-backend:latest`
- CPU 256, memory 512
- Port mappings: 8000.
- Environment: `ENV=dev`, `AWS_REGION=us-east-1`, `AUTH_MODE=prod`, `TEMPORAL_MODE=real`, `CORS_ORIGINS=["https://nexus.<dominio>"]`.
- Secrets: `MONGODB_URI` ← `secrets/nexus/dev/mongodb_uri`, `REDIS_URL` ← `secrets/nexus/dev/redis_url`, `TEMPORAL_HOST` ← `secrets/nexus/dev/temporal_ngrok_url`.
- Parameters: `COGNITO_USER_POOL_ID`, `COGNITO_APP_CLIENT_ID`, `COGNITO_JWKS_URL`, `S3_RECEIPTS_BUCKET`, `S3_TEXTRACT_OUTPUT_BUCKET`.

**Task def frontend:**
- Container: `<account>.dkr.ecr.us-east-1.amazonaws.com/nexus-frontend:latest`
- CPU 256, memory 512
- Port 3000.
- Environment: `NEXT_PUBLIC_API_BASE_URL=https://nexus.<dominio>/api`, `NEXT_PUBLIC_COGNITO_DOMAIN=...`, `NEXT_PUBLIC_COGNITO_CLIENT_ID=...`.

**Services:**
- `aws_ecs_service` backend: desired_count=1, launch_type=FARGATE, subnets=privadas, security_group permite 8000 desde ALB SG, `load_balancer` conectado al backend TG.
- `aws_ecs_service` frontend: idem pero puerto 3000, TG frontend.

**Validación:**
1. `aws ecs list-tasks --cluster nexus-dev-edgm` → muestra 2 tasks RUNNING.
2. `curl https://<alb-dns>/healthz` → `{"status":"ok"}` del backend.
3. `curl https://<alb-dns>/` → HTML de Next.js.

---

### Fase A.9 · Temporal via ngrok

**Qué:** exponer el Temporal local al backend en AWS.

**En tu Mac:**
```bash
# Instalar ngrok si no lo tienes
brew install ngrok
ngrok config add-authtoken <token-de-ngrok.com>

# Abrir tunnel TCP al puerto Temporal
ngrok tcp 7233 --region=us
# Output: Forwarding tcp://4.tcp.ngrok.io:12345 -> localhost:7233
```

**Copiar la URL al secret:**
```bash
aws secretsmanager put-secret-value \
  --secret-id nexus/dev/temporal_ngrok_url \
  --secret-string "4.tcp.ngrok.io:12345"
```

**Reiniciar backend para que tome el nuevo valor:**
```bash
aws ecs update-service --cluster nexus-dev-edgm --service nexus-backend --force-new-deployment
```

**Cómo mantenerlo:** el tunnel se cae si cierras terminal. Para demos largas usa `ngrok config file` con `tunnels` persistentes + `ngrok start --all`, o lo lanzas en un `tmux` / background.

**Limitación conocida:** ngrok free tier cambia la URL cada vez que lo reinicias. Work-around: plan ngrok "Personal" $10/mo da URL estática. Para demo puntual, free basta.

**Validación:** subir un recibo desde el frontend en AWS → workflow llega a Temporal local → aparece en `http://localhost:8233` (Temporal UI local).

---

### Fase A.10 · CI/CD de deploys

**Qué:** push a `main` → deploy automático a ECS.

**Extender `.github/workflows/build-and-push.yml`** (de Phase A.5) con un step final:
```yaml
- name: Force new ECS deployment
  run: |
    aws ecs update-service --cluster nexus-dev-edgm \
      --service nexus-${{ matrix.service }} \
      --force-new-deployment
```

**Alternativa más robusta:** crear una nueva task definition con el tag específico y rolling update. Para Phase A, `:latest` + `--force-new-deployment` basta.

**Añadir a IAM del role OIDC GHA:** `ecs:UpdateService`, `ecs:RegisterTaskDefinition`, `iam:PassRole` (sobre los task roles).

**Validación:** push en `backend/src/...` → GHA corre → nueva imagen en ECR → service se recrea → health check pasa.

---

## 5 · Cambios necesarios en código (fuera de IaC)

El código actual asume `AUTH_MODE=dev` y localhost. Cambios mínimos para que funcione en AWS:

### Backend
- **Nada** si `AUTH_MODE=prod` y el JWKS URL de Cognito están bien en env. El backend ya soporta ambos modos.
- Verificar que `CORS_ORIGINS` incluya el dominio del ALB.
- Dockerfile ya funciona en Fargate. `CMD uvicorn --host 0.0.0.0 --port 8000` ya está bien.

### Frontend (Next.js)
- Reemplazar el login local por **Cognito Hosted UI**:
  - Añadir `@aws-amplify/auth` o `oidc-client-ts` (recomendado: `next-auth` con el provider Cognito).
  - Flow: click "Login" → redirige a `https://<cognito-domain>/oauth2/authorize?...` → callback a `/auth/callback` del frontend → intercambia code por tokens → guarda en httpOnly cookie.
  - El `Authorization: Bearer <id_token>` se envía al backend como hoy.
- **Tenant ID**: leer del JWT (`custom:tenant_id`). El backend espera este claim.
- Dockerfile nuevo (si no existe aún):
  ```dockerfile
  FROM node:20-alpine AS builder
  WORKDIR /app
  COPY package*.json ./
  RUN npm ci
  COPY . .
  RUN npm run build
  FROM node:20-alpine
  WORKDIR /app
  COPY --from=builder /app/.next ./.next
  COPY --from=builder /app/node_modules ./node_modules
  COPY --from=builder /app/package.json ./
  EXPOSE 3000
  CMD ["npm", "start"]
  ```

### Configuración por entorno
- Crear un `.env.production` en frontend con las vars `NEXT_PUBLIC_*` inyectadas en build time por el CI.
- Backend: leer todo de env (ya lo hace), los valores los pone ECS desde Secrets + SSM.

---

## 6 · Checklist de validación final

Para cerrar Phase A y pasar a B, todo esto debe pasar:

- [ ] `curl https://<dominio>/healthz` → 200 `{"status":"ok","deps":{"mongo":"ok","redis":"ok","temporal":"ok"}}`.
- [ ] Abrir `https://<dominio>/` en navegador → redirige a Cognito → sign-up → confirm → redirige de vuelta → dashboard visible.
- [ ] Upload de un PDF vía la UI → aparece en el S3 bucket de receipts real.
- [ ] `workflow.started` llega al SSE del frontend.
- [ ] Temporal local (en Mac) muestra el workflow en la UI (`http://localhost:8233`).
- [ ] Con `FAKE_PROVIDERS=true` en workers locales, el flujo termina en `workflow.hitl_required` (o `workflow.completed` con `FAKE_HITL_MODE=never`).
- [ ] CloudWatch Logs muestra logs estructurados de backend y frontend.
- [ ] Push a `main` en backend → ECS despliega nueva versión sin downtime (health checks se cumplen).
- [ ] `terraform destroy -target=aws_ecs_service.backend` apaga el backend y no rompe nada más; `terraform apply` lo vuelve a levantar.
- [ ] Costo proyectado en `https://console.aws.amazon.com/billing/home#/bills` < $5 para la primera semana.

---

## 7 · Rollback / cleanup

**Apagar todo temporalmente** (conservando network, Cognito, Atlas):
```bash
cd infra/terraform
terraform destroy \
  -target=aws_ecs_service.backend \
  -target=aws_ecs_service.frontend \
  -target=aws_elasticache_cluster.redis
```

**Destruir Phase A completa:**
```bash
terraform destroy -target=module.phase_a  # si modularizamos
# o resource por resource
```

**Cosas a borrar a mano (no las maneja TF):**
- Imágenes en ECR (ignorar — lifecycle policy las limpia).
- Users creados en Cognito (si queremos dejarlo limpio).
- Cluster M0 de Atlas (UI).
- Tunnel ngrok (ctrl-c).
- Logs en CloudWatch (TF los borra con el log group, pero pueden quedar en estado `DELETING`).

---

## 8 · Gotchas conocidos

1. **Fargate cold start**: ~30s desde `aws ecs run-task` hasta listen. Health check grace period del TG: `health_check_grace_period_seconds = 60` en el service.
2. **SSE + ALB**: Server-Sent Events requiere *sticky sessions* en el target group (`stickiness { enabled = true, type = "lb_cookie", cookie_duration = 3600 }`) y `idle_timeout > 60s` en el ALB, porque SSE mantiene la conexión abierta. Sin esto, los events llegan entrecortados.
3. **Cognito callback URLs**: cuando creas el app client el callback URL debe incluir el ALB DNS **después** de Fase A.7. Un primer apply crea el pool sin callbacks válidos; el segundo apply los añade. Dos-pasos-apply es normal aquí.
4. **ngrok free tier**: la URL cambia cada reinicio. Guionar un hook post-start que actualice el secret automáticamente si vas a hacer demos frecuentes:
   ```bash
   NGROK_URL=$(curl -s localhost:4040/api/tunnels | jq -r '.tunnels[0].public_url' | sed 's|tcp://||')
   aws secretsmanager put-secret-value --secret-id nexus/dev/temporal_ngrok_url --secret-string "$NGROK_URL"
   aws ecs update-service --cluster ... --service nexus-backend --force-new-deployment
   ```
5. **MongoDB Atlas M0 limits**: 512 MB storage, 100 connections. Para demo está bien. Si empiezas a ver "too many connections", subí a M2 ($9/mo) o M10 ($57/mo). Phase B *requiere* M10+.
6. **ECS task role vs execution role**: la distinción es frecuente fuente de errores. El **execution role** es el que usa el agente ECS para jalar la imagen y escribir logs. El **task role** es el que usa tu aplicación adentro para hablar con AWS. Secrets Manager y SSM necesitan permisos en el **execution role** si se inyectan via `secrets:` en la task def, y en el **task role** si los lees desde código. Elegí la via `secrets:` → permisos solo en execution role.
7. **VPC endpoints**: cada `boto3` call desde ECS a AWS (S3, Secrets, SSM) pasa por NAT por default (cuesta egress). Se puede ahorrar ~$5/mo añadiendo VPC endpoints para S3 (gateway, gratis) y Secrets Manager (interface, $7/mo — no ahorra a este tamaño). Solo vale la pena el gateway endpoint de S3 en Phase A. Añadir en `network.tf`.

---

## 9 · Qué queda fuera de Phase A (referencias para fases siguientes)

- **Textract real**: bloqueado esperando activación de cuenta. Cuando se active, solo cambiamos `FAKE_PROVIDERS=false` en el .env del worker local — ya tenemos S3 real y IAM correctos.
- **Bedrock real**: el chatbot RAG sigue usando el fake en el worker local. Para activarlo en AWS necesitamos solicitar acceso a Bedrock en la consola (no automatizable) y mover el worker de RAG a AWS (Phase B tardío o C).
- **CDC MongoDB → Bronze**: Phase B. Requiere Atlas M10+.
- **Databricks workspace**: Phase C. Workspace propio + Unity Catalog + pipelines Lakeflow.
- **Observability avanzada**: OTel → X-Ray o Tempo. Phase B.
- **WAF + Shield**: no necesario para demo.
- **Multi-region / DR**: no necesario para demo.

---

## 10 · Referencias útiles

- ECS Fargate task sizes + pricing: https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-tasks-services.html
- Cognito OAuth flows: https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-app-integration.html
- ALB + ECS service discovery: https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-load-balancing.html
- MongoDB Atlas IP access list: https://www.mongodb.com/docs/atlas/security/ip-access-list/
- ngrok TCP tunnels: https://ngrok.com/docs/tcp/
- Terraform AWS provider ECR + ECS examples: https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/ecs_service

---

**Próximo paso al confirmar este plan:** empiezo por **Fase A.0 (scaffolding)** creando todos los `.tf` nuevos con estructura vacía validable, y te muestro el diff antes de cualquier `apply`.
