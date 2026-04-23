# Phase A — Guía de tecnologías (para estudiar)

Este documento describe todas las piezas que se levantaron en Phase A. Para cada una: **qué es**, **cómo se usa en Nexus**, y **qué problemas de seguridad resuelve**. Úsalo para preparar la entrevista.

Mapa mental rápido de la arquitectura levantada:

```
Internet → ALB (HTTP/HTTPS) ─┬─► Frontend ECS task (Next.js :3000)
                             └─► Backend ECS task (FastAPI :8000)
                                   ├─► MongoDB Atlas (SaaS en AWS us-east-1)
                                   ├─► ElastiCache Redis (VPC privada)
                                   ├─► S3 (receipts + textract-output)
                                   ├─► Secrets Manager / SSM
                                   └─► (Futuro) Temporal vía ngrok
Cognito User Pool: autentica usuarios → el backend valida JWT contra JWKS
GitHub Actions: OIDC → asume rol IAM → docker push a ECR → ECS redeploy
Todo orquestado por Terraform.
```

---

## 1. VPC (Virtual Private Cloud)

**Qué es:** red virtual privada dentro de AWS, aislada de las VPCs de otros clientes. Se define por un rango CIDR (`10.0.0.0/16` en nuestro caso) y contiene subnets, route tables y gateways. Todo recurso AWS que corre en red (ECS, RDS, ElastiCache, etc.) vive dentro de una VPC.

**En Nexus:** una sola VPC en `us-east-1`, dos zonas de disponibilidad (`us-east-1a`, `us-east-1b`) para soportar HA si luego lo necesitamos. Cuatro subnets:
- 2 **públicas** (`10.0.1.0/24`, `10.0.2.0/24`) — aquí viven el ALB y el NAT Gateway. Tienen ruta por defecto al Internet Gateway, pueden recibir tráfico directo de internet.
- 2 **privadas** (`10.0.11.0/24`, `10.0.12.0/24`) — aquí viven las ECS tasks y Redis. Sin IP pública. Para salir a internet pasan por NAT.

**Seguridad que resuelve:**
- **Aislamiento de red**: el ALB es lo único expuesto a internet; backend y Redis son inaccesibles desde fuera sin pasar por el ALB.
- **Defensa en profundidad**: aun si un atacante escanea tu cuenta AWS, no puede hablarle directo a una task ECS — tiene que pasar por el ALB + reglas del listener + SG.
- **Cumplimiento**: la separación pública/privada es requisito en la mayoría de marcos (PCI, HIPAA, SOC2).

---

## 2. Internet Gateway (IGW) + NAT Gateway

**Qué son:**
- **IGW**: puerta bidireccional que conecta la VPC con internet. Recursos en subnets públicas con IP pública pueden hablarle al mundo y recibir tráfico entrante.
- **NAT Gateway**: traductor de direcciones. Recursos en subnets **privadas** salen a internet "por detrás" del NAT, que tiene una IP pública única (`54.84.212.155` en nuestro caso). De afuera nadie puede iniciar una conexión hacia la task privada — solo pueden responder a conexiones que la task originó.

**En Nexus:** el NAT lo usa el backend para llamar APIs externas (Cognito, Secrets Manager, S3, MongoDB Atlas, ngrok). Como solo hay 1 NAT (ahorro ~$32/mo vs 1 por AZ), es punto único de falla — aceptable en dev.

**Seguridad que resuelve:**
- **Unidireccionalidad del tráfico**: internet no puede iniciar conexión a las ECS tasks. Solo las tasks inician y el NAT devuelve la respuesta.
- **IP estable para egress**: el IP del NAT (`54.84.212.155`) es conocido y estable, permite whitelisting en terceros (ej. MongoDB Atlas solo acepta conexiones desde `54.84.212.155/32`).
- **Evita exposición accidental**: aun si alguien configura mal una task con un servidor escuchando en `0.0.0.0:9090`, nadie desde fuera puede llegar.

---

## 3. Route Tables + S3 Gateway Endpoint

**Qué son:**
- **Route Tables**: reglas de enrutamiento por subnet. Pública → default al IGW. Privada → default al NAT.
- **VPC Endpoint (Gateway) para S3**: permite que las tasks privadas hablen con S3 **sin salir por NAT**. El tráfico nunca toca internet, queda dentro de la red AWS. Gratis.

**En Nexus:** las subnets privadas tienen ruta a S3 que va por el endpoint en vez del NAT. Ahorra egress ($0.09/GB) y reduce latencia.

**Seguridad que resuelve:**
- **Tráfico de datos sensibles no sale de AWS**: si el backend sube un PDF con datos del usuario a S3, ese tráfico no transita por internet público. Nunca deja la fibra interna de AWS.
- **Política de endpoint**: se puede restringir el VPC endpoint a solo ciertos buckets para evitar exfiltración a buckets externos.

---

## 4. Security Groups

**Qué son:** firewalls *stateful* a nivel de instancia/ENI. Cada SG define reglas `ingress` (quién puede entrar) y `egress` (a dónde puede salir). "Stateful" = si una conexión sale, la respuesta entra automáticamente sin regla explícita.

**En Nexus:** 4 SGs encadenados:
- `alb-sg`: ingress 80/443 desde `0.0.0.0/0` (mundo).
- `backend-sg`: ingress 8000 **solo desde `alb-sg`** (referencia a otro SG, no IPs).
- `frontend-sg`: ingress 3000 **solo desde `alb-sg`**.
- `redis-sg`: ingress 6379 **solo desde `backend-sg`**.

**Seguridad que resuelve:**
- **Principio de menor privilegio en red**: cada servicio solo recibe conexiones de quien realmente necesita hablarle.
- **Referencias por SG (no IP)**: cuando una task ECS se relanza con IP nueva, las reglas siguen funcionando porque referencian al SG, no al IP. Inmune a IP churn.
- **Segmentación**: si alguien compromete el frontend (XSS, RCE), **no puede hablarle directo a Redis**, porque el SG de Redis no confía en `frontend-sg`.

---

## 5. Amazon Cognito (User Pool)

**Qué es:** servicio de identidad de AWS. Maneja registro, login, recuperación de password, MFA, federación social (Google, Facebook, SAML, OIDC). Emite JWTs (`id_token`, `access_token`, `refresh_token`) firmados con RS256.

**En Nexus:**
- Un **User Pool** (`us-east-1_0tOMlslMI`) con signup habilitado y email verification.
- Un **App Client** con OAuth flow `code` (authorization code + PKCE), scopes `openid profile email`.
- Un **Hosted UI domain** (`nexus-dev-edgm-auth.auth.us-east-1.amazoncognito.com`) — UI de login lista, no tienes que escribir formulario.
- Un **atributo custom `tenant_id`** que el backend lee del JWT para filtrar datos por tenant (multi-tenancy).
- El backend valida los JWTs contra la **JWKS URL** (lista pública de llaves públicas de Cognito).

**Flujo completo:**
```
1. Usuario clickea "Login" → frontend hace signInWithRedirect (amplify)
2. Redirect a https://nexus-dev-edgm-auth.auth.us-east-1.amazoncognito.com/login?client_id=...&response_type=code
3. Usuario mete email + password → Cognito redirige a /auth/callback?code=XXX
4. Amplify intercambia code por tokens en /oauth2/token (PKCE protege el intercambio)
5. Frontend guarda id_token → cada request al backend lleva Authorization: Bearer <id_token>
6. Backend descarga JWKS (cachea), valida firma + expiración + issuer + audience
7. Backend extrae sub (user id) + custom:tenant_id del payload → lo usa para filtrar queries
```

**Seguridad que resuelve:**
- **Tú no almacenas passwords**: Cognito los guarda hasheados con SRP. Si te comprometen la app no filtras passwords.
- **MFA opt-in**: se activa con una llamada API, sin reescribir el login.
- **JWT firmado asimétricamente (RS256)**: el backend valida con llave pública; si alguien te roba el código del backend no puede firmar tokens porque no tiene la privada (vive solo en Cognito).
- **Expiración corta (1h access token, 1h id token, 30d refresh)**: un token robado tiene ventana limitada.
- **`prevent_user_existence_errors`**: Cognito devuelve el mismo mensaje cuando el email existe pero el password es malo vs cuando el email no existe. Previene enumeración de usuarios.
- **OAuth 2.0 `code` flow + PKCE**: el token nunca pasa por la URL del browser, solo el `code` (un ticket de un solo uso). Protege contra interceptación de tokens.

---

## 6. ECR (Elastic Container Registry)

**Qué es:** registro privado de imágenes Docker dentro de AWS. Equivalente a Docker Hub pero privado y autenticado con IAM.

**En Nexus:** dos repos: `nexus-dev-edgm-backend`, `nexus-dev-edgm-frontend`. Config:
- `scan_on_push = true` → analiza vulnerabilidades conocidas (CVEs) apenas subes una imagen.
- `image_tag_mutability = "IMMUTABLE"` → una vez subes `:abc123`, no puedes sobreescribir ese tag con otra imagen (solo `:latest` se comporta raro, ver gotcha en push).
- Lifecycle policy que retiene las **20 imágenes más recientes** para no pagar storage indefinido.

**Seguridad que resuelve:**
- **Supply chain**: escaneo de CVEs en cada push (detecta libs vulnerables como log4shell, openssl CVEs).
- **Reproducibilidad**: tags inmutables = si tu deploy apunta a `:abc123` **estás garantizado** que es la misma imagen siempre. Un atacante no puede re-publicar un `:abc123` con una backdoor.
- **Autenticación IAM**: nadie fuera de tu cuenta AWS puede descargar tu imagen (contiene tu código propietario).

---

## 7. ElastiCache Redis

**Qué es:** Redis gestionado por AWS. Tú solo eliges versión, tamaño de instancia y subnets; AWS se encarga de updates, failover, backups.

**En Nexus:** `cache.t4g.micro`, Redis 7.1, 1 nodo (no HA en dev), en subnets privadas. Uso:
- **Pub/Sub** para Server-Sent Events (SSE): workers Temporal publican eventos → backend los reenvía por SSE al frontend (`nexus:events:tenant:{t}:user:{u}`).
- **Replay buffer**: si un usuario refresca la página, el backend le reenvía los últimos 50 eventos del sorted set (sobrevive desconexiones).
- **Rate limiting** potencial (no implementado aún).

**Seguridad que resuelve:**
- **Aislamiento de red**: solo accesible desde el SG del backend (puerto 6379). Imposible desde internet.
- **Gestionado**: parches de CVEs de Redis los aplica AWS automáticamente (durante ventanas de mantenimiento).
- **Encryption at-rest + in-transit**: opcionales con un flag; por defecto off en `t4g.micro` dev — se activaría en prod.

---

## 8. Secrets Manager + SSM Parameter Store

**Qué son:**
- **Secrets Manager**: almacén para datos sensibles (DB passwords, API keys). Rotación automática, versionado, auditoría vía CloudTrail. **Cobra $0.40/secreto/mes + $0.05 cada 10k lookups.**
- **SSM Parameter Store**: almacén para config no-sensible. Gratis (hasta 10k params estándar).

**En Nexus:**
- **Secrets**: `mongodb_uri`, `redis_url`, `temporal_ngrok_url` (URLs con credenciales o que cambian).
- **SSM**: `aws_region`, `s3_receipts_bucket`, `cognito_user_pool_id`, `cognito_client_id`, `cognito_jwks_url` (config pero no secretos).
- ECS los **inyecta en la task al arrancar** (no están en la imagen ni en variables .env que podrían terminar en git): el Task Execution Role lee el secret al lanzar la task y lo mete como env var en el container.

**Seguridad que resuelve:**
- **Secretos fuera del código**: el `mongodb+srv://user:password@...` no vive en ningún repo ni imagen. Git secrets / accidentes de `.env` commiteados se eliminan.
- **Auditoría**: CloudTrail registra quién leyó qué secret y cuándo. Útil para compliance y forense si hay una filtración.
- **Rotación**: si el password se compromete, rotas el secret con una API call y todas las tasks lo toman en el próximo deploy, sin cambiar código.
- **IAM fine-grained**: el Task Execution Role solo puede leer los 3 secretos de Nexus, no todos los de la cuenta.

---

## 9. Application Load Balancer (ALB)

**Qué es:** balanceador layer-7 (HTTP/HTTPS). Entiende URLs, headers, cookies. Hace health checks, sticky sessions, path/host routing.

**En Nexus:**
- 1 ALB público en subnets públicas, 2 AZs para HA.
- 3 **target groups** (grupos de destinos):
  - `backend-tg` → port 8000, health `/healthz`.
  - `backend-sse-tg` → port 8000 con **sticky sessions** (cookie de AWS) para SSE. Una conexión SSE no puede rotar de task, o el cliente pierde eventos.
  - `frontend-tg` → port 3000, health `/`.
- Listener HTTP :80 + reglas path-based:
  - `/api/v1/events/*` y `/api/v1/chat/stream/*` → `backend-sse-tg` (priority 5).
  - `/api/*` y `/healthz` → `backend-tg` (priority 10).
  - Default → `frontend-tg`.
- `idle_timeout = 120s` para mantener SSE vivos.

**Seguridad que resuelve:**
- **Terminación TLS centralizada**: cuando activemos HTTPS, el cert ACM se monta en el ALB, no hay que distribuirlo a cada task.
- **WAF-ready**: se le puede asociar un AWS WAF para rate-limit, bot mitigation, OWASP rules en una línea de Terraform.
- **Protección DDoS**: AWS Shield Standard incluido gratis, absorbe SYN floods y UDP reflection en la capa de red.
- **Aislamiento de tasks**: las tasks solo tienen IP privada. El ALB es el único punto público.
- **Health checks**: una task que responde 500 es sacada del pool automáticamente (no se cae el servicio completo).

---

## 10. ECS Fargate + IAM Roles

**Qué es:**
- **ECS (Elastic Container Service)**: orquestador de contenedores de AWS. Tú le das una **task definition** (imagen + CPU/mem + env vars + IAM role) y un **service** (desired_count, health checks, load balancer), él se encarga de correrlos.
- **Fargate**: modo *serverless* de ECS. Tú no administras servidores; AWS provisiona la VM, corre el container y te cobra por vCPU/GB-hora. Alternativa: ECS on EC2 (tú manejas las instancias).

**En Nexus:** cluster `nexus-dev-edgm`. Task defs:
- Backend: 256 CPU / 512 MB, ~9 env vars + 3 secrets inyectados desde Secrets Manager.
- Frontend: 256 CPU / 512 MB, env vars de Next.js.

**Tres IAM roles distintos — importante:**
1. **Task Execution Role** (`nexus-dev-edgm-ecs-task-execution`): lo usa el **agente ECS** (de AWS, no tu código) para hacer `docker pull` de ECR, escribir logs a CloudWatch, y resolver los secrets para inyectarlos al container. Tu código **no** ve este rol.
2. **Task Role backend** (`nexus-dev-edgm-ecs-task-backend`): lo usa **tu código** (boto3 dentro del container). Permisos: `s3:*` sobre los 2 buckets, `textract:AnalyzeExpense`, `cognito-idp:AdminGetUser`.
3. **Task Role frontend**: casi vacío (Next.js no llama APIs AWS directamente).

**Seguridad que resuelve:**
- **Principio de menor privilegio**: tu código solo puede hacer `s3:PutObject` sobre sus buckets. No puede borrar otro bucket, no puede leer secrets de otra app, no puede crear EC2 instances.
- **Sin credenciales hardcoded**: boto3 dentro de la task resuelve credenciales del task role via IMDSv2 automáticamente. No hay `AWS_ACCESS_KEY_ID` ni archivos `~/.aws/credentials` en la imagen.
- **Separación execution vs task role**: aun si tu código es comprometido, solo puede hacer lo del task role (no leer secrets que no consume, no pullear otras imágenes).
- **Aislamiento container/VM**: Fargate te da una VM por task (Firecracker microVM), no comparte kernel con otros tenants.
- **Versionado de task definition**: cada cambio crea una nueva revision; rollback = apuntar service a revision anterior.

---

## 11. CloudWatch Logs

**Qué es:** servicio de logs gestionado. Cada container ECS puede emitir stdout/stderr a CloudWatch directamente (driver `awslogs`).

**En Nexus:** dos log groups: `/ecs/nexus-dev-edgm/backend` y `/ecs/nexus-dev-edgm/frontend`. Retención 14 días (ahorra costo, ajustable).

**Seguridad que resuelve:**
- **Logs centralizados**: si la task muere, los logs ya están en CloudWatch (no se pierden con el container).
- **Auditoría**: los logs son append-only, no los puede modificar una task comprometida.
- **Alarmas**: se pueden disparar CloudWatch Alarms sobre patrones (ej. `ERROR`, `5xx`) → notificaciones SNS, PagerDuty, etc.
- **Compliance**: retención configurable para cumplir requerimientos (SOC2 suele exigir 90d+).

---

## 12. IAM + OIDC para GitHub Actions

**Qué es:**
- **IAM (Identity and Access Management)**: servicio de AWS que define identidades (users, roles) y permisos (policies).
- **OIDC (OpenID Connect) provider**: mecanismo donde AWS confía en un emisor externo (GitHub) para emitir JWTs que permiten asumir un role sin access keys.

**En Nexus:**
- Un **OIDC provider** que confía en `token.actions.githubusercontent.com`.
- Un **IAM role** (`nexus-dev-edgm-gha-terraform`) con trust policy que solo acepta JWTs que cumplan:
  - `aud = sts.amazonaws.com`
  - `sub = repo:einsteindark-edgm/casaware_exercise:ref:refs/heads/main` (o `:pull_request`).
- Inline policy del role con permisos mínimos: S3 sobre los buckets del proyecto, ECR push/pull sobre los 2 repos, ECS update-service/register-task-definition, `iam:PassRole` para pasar los task roles.

**Flujo:**
```
1. GHA corre workflow → GitHub firma un OIDC JWT con claims {repo, ref, sub, aud}
2. GHA step aws-actions/configure-aws-credentials@v4 llama sts:AssumeRoleWithWebIdentity
3. AWS valida el JWT contra la OIDC provider config (firma, claims)
4. Si todo cuadra, GHA recibe credenciales temporales (1h)
5. Con esas credenciales hace docker push + aws ecs update-service
```

**Seguridad que resuelve:**
- **Cero access keys de larga duración en GitHub Secrets**: el JWT es emitido por branch específico, expira en 1h. Si alguien roba los logs no puede reusar nada.
- **Trust policy granular**: el role solo puede ser asumido desde el repo y branch específicos. Si un fork hace un PR, no puede asumir el rol.
- **Blast radius limitado**: la policy inline del rol solo permite lo estrictamente necesario para build+deploy de Nexus. No `s3:*`, no `ec2:*`, no "AdministratorAccess".
- **Rotación automática**: las creds de STS expiran en 1h. Nada que rotar manualmente.

---

## 13. S3 (receipts + textract-output)

**Qué es:** almacenamiento de objetos de AWS. Durabilidad 11 nueves, URL globalmente único por bucket.

**En Nexus:** dos buckets:
- `nexus-dev-edgm-receipts`: PDFs/imágenes de recibos subidos por el usuario. Versionado habilitado.
- `nexus-dev-edgm-textract-output`: JSON crudo que Textract devuelve tras analizar un recibo (lineage). Lifecycle expira objetos a los 30 días.

Ambos con:
- **Server-Side Encryption** (SSE-S3, AES-256 con llaves gestionadas por AWS).
- **Public Access Block** total: ni ACL ni policy pueden volver el bucket público.
- **CORS** solo para `http://localhost:3000` (dev) — se amplía para el ALB cuando probemos subidas desde la UI deployada.

**Seguridad que resuelve:**
- **Encryption-at-rest por default**: ningún objeto se persiste sin cifrar.
- **Bloqueo de exposición pública accidental**: el Public Access Block previene que alguien (humano o script) convierta el bucket público por error (el caso típico de leaks de S3).
- **Versionado**: si un bug borra un objeto, la versión anterior sigue ahí (recuperable).
- **Lifecycle**: los outputs de Textract tienen datos sensibles; expirarlos a 30d limita el window de exposición si el bucket se comprometiera.
- **Acceso solo via IAM**: sin firma IAM nadie descarga nada. Acceso por presigned URLs (que tu código genera con TTL corto) para el download del usuario.

---

## 14. MongoDB Atlas (SaaS, corre en AWS us-east-1)

**Qué es:** Mongo gestionado por MongoDB Inc., desplegado dentro de AWS. No es DocumentDB (compatible parcial); es Mongo real.

**En Nexus:**
- Cluster M0 (free tier): 512 MB storage, suficiente para dev.
- Usuario `einsteindark_db_user` con role `readWrite` sobre `nexus_dev`.
- **IP Access List** restringida al NAT de nuestra VPC (`54.84.212.155/32`). Nadie más puede hablarle.
- Connection string en Secrets Manager.

**Seguridad que resuelve:**
- **IP whitelisting**: solo nuestro NAT puede conectar. Un atacante que robe el password pero no esté en nuestra IP no entra.
- **TLS por default**: todas las conexiones Atlas son TLS 1.2+.
- **Usuario con scope limitado**: el user solo tiene `readWrite` sobre `nexus_dev`, no `root` ni `dbAdmin`.
- **Managed patching**: Mongo se parchea automáticamente en la ventana configurada.

**Nota Phase B:** al migrar a M10 ($57/mo) activamos **VPC Peering** entre nuestra VPC y la de Atlas — el tráfico queda dentro de AWS, sin pasar por IP pública. Debezium puede leer Change Streams para CDC.

---

## 15. Terraform (IaC)

**Qué es:** herramienta de Infrastructure-as-Code. Defines recursos AWS en HCL, `terraform apply` ejecuta las APIs para llevar tu cuenta al estado deseado. Guarda un **state file** con lo que existe para planear diffs.

**En Nexus:** `infra/terraform/` con 14 archivos `.tf`. State local (en Phase B lo moveremos a S3 + DynamoDB lock). El IAM role OIDC ya está preparado para que GHA haga `terraform apply` de forma automática cuando pasemos a state remoto.

**Seguridad que resuelve:**
- **Reproducibilidad**: toda la infra se puede recrear en otra cuenta con un `apply`. Defensa contra ransomware que bloquee tu cuenta.
- **Auditoría del diseño**: cada cambio de infra pasa por PR review. Nadie cambia un SG a mano sin dejar rastro.
- **Drift detection**: `terraform plan` detecta si alguien modificó algo manualmente.
- **Secretos fuera del state**: usamos `lifecycle { ignore_changes = [secret_string] }` para que los valores reales de Secrets Manager no terminen en el state file.

---

## 16. Docker multi-stage + buildx

**Qué es:**
- **Docker multi-stage**: patrón donde el `Dockerfile` tiene varias etapas (`FROM ... AS deps`, `FROM ... AS builder`, `FROM ... AS runner`). Solo la última etapa termina en la imagen final → tamaño menor, superficie menor.
- **buildx**: builder de Docker con soporte multi-plataforma (arm64, amd64). Fargate corre `x86_64`, así que forzamos `--platform linux/amd64` desde nuestra Mac Apple Silicon.

**En Nexus:**
- Backend: base `python:3.12-slim` + `uv` para instalar deps, multi-stage pequeño.
- Frontend: `node:22-alpine` multi-stage: deps → builder (`npm run build`) → runner (solo `.next/standalone` + `public/` + `node_modules` mínimas).

**Seguridad que resuelve:**
- **Superficie reducida**: la imagen final no tiene compiladores, `npm`, ni devDependencies. Menos binarios = menos vectores de RCE.
- **Imagen non-root**: el frontend corre como user `nextjs` (UID 1001), no root. Un escape de container es menos catastrófico.
- **Reproducibilidad de builds**: lockfiles (`uv.lock`, `package-lock.json`) garantizan las mismas versiones en cada build.
- **Alpine/slim base**: imagen base mínima, menor probabilidad de heredar CVEs.

---

## 17. Server-Sent Events (SSE) sobre ALB con sticky sessions

**Qué es:** **SSE** es un protocolo HTTP para server → cliente streaming (unidireccional). Más simple que WebSockets, va sobre HTTP normal. Ideal para empujar eventos de workflow al frontend en tiempo real.

**En Nexus:** el backend expone `/api/v1/events/stream` que mantiene una conexión abierta y pushea eventos (`workflow.ocr_progress`, `workflow.hitl_required`, etc.) conforme Redis los publica.

**Problema con ALB:** si hay 2 tasks backend, el ALB puede enrutar una request SSE a task-A y el cliente reconecta cayendo en task-B → pierde la cola Redis subscribe de A.

**Solución aplicada:** un target group **separado** (`backend-sse-tg`) con `stickiness.enabled = true` + cookie `AWSALB` de 1h. Cada cliente queda pegado a la misma task durante su sesión.

**Seguridad que resuelve:**
- **Consistencia**: el usuario recibe eventos en orden sin duplicados (siempre la misma task le empuja).
- **Aislamiento de conexión**: si el HTTP header `Authorization` se validó en task-A, todos los eventos vienen del mismo contexto autenticado.

---

## Cheat sheet de conceptos de seguridad implementados

Agrupando los temas que puedes mencionar en la entrevista:

| Concepto | Dónde se aplica en Nexus |
|---|---|
| **Principio de menor privilegio (PoLP)** | IAM roles (execution vs task vs GHA), SGs encadenados |
| **Defensa en profundidad** | VPC pública/privada + SG + IAM + encryption + app auth |
| **Zero trust en tráfico interno** | SG por SG (no por IP), Cognito JWT en cada request |
| **Secretos fuera de código** | Secrets Manager + inyección en runtime, no en imagen |
| **Auditoría** | CloudTrail + CloudWatch logs append-only |
| **Encryption** | S3 SSE, TLS en Atlas, Redis in-transit opcional |
| **Supply chain security** | ECR scan on push + lockfiles + imágenes inmutables |
| **Identidad federada sin long-lived keys** | OIDC GitHub → IAM role assumption (1h STS creds) |
| **Multi-tenancy** | `custom:tenant_id` en JWT Cognito → filtrado en backend |
| **Rate limiting / DDoS** | ALB + Shield Standard, WAF-ready |
| **OWASP** | Cognito (auth), SGs (2 - broken auth), SSE tokens en header (3 - sensitive data), input validation pydantic (4 - injection), imagen scans (6 - vulns) |

---

## Recursos para estudiar más a fondo

Documentos oficiales AWS que sirven para entrevista:

- **VPC**: https://docs.aws.amazon.com/vpc/latest/userguide/what-is-amazon-vpc.html
- **IAM best practices**: https://docs.aws.amazon.com/IAM/latest/UserGuide/best-practices.html
- **Cognito**: https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-identity-pools.html
- **ECS Fargate**: https://docs.aws.amazon.com/AmazonECS/latest/developerguide/AWS_Fargate.html
- **ALB**: https://docs.aws.amazon.com/elasticloadbalancing/latest/application/introduction.html
- **Secrets Manager**: https://docs.aws.amazon.com/secretsmanager/latest/userguide/intro.html
- **OIDC GitHub → AWS**: https://docs.github.com/en/actions/deployment/security-hardening-your-deployments/configuring-openid-connect-in-amazon-web-services
- **Well-Architected Framework — Security pillar**: https://docs.aws.amazon.com/wellarchitected/latest/security-pillar/welcome.html

Para entrevista, practica estas preguntas:
1. ¿Por qué subnets privadas en vez de públicas con SG?
2. ¿Qué pasa si alguien filtra el access key del worker IAM user?
3. ¿Cómo previenes que un atacante suba una imagen con backdoor a ECR?
4. ¿Cómo rotarías el password de Mongo sin downtime?
5. ¿Qué hace diferente al Task Execution Role del Task Role?
6. ¿Cómo detectarías si un atacante exfiltra datos por el NAT?
7. ¿Por qué el id token en Cognito y no el access token para autorizar?
8. ¿Qué pasa si el NAT Gateway se cae?
