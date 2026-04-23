# Nexus — Roadmap de despliegue en AWS

Plan incremental para llevar el proyecto Nexus de "funciona en mi Mac" a "funciona en AWS real". Tres fases ordenadas por dependencia y costo.

| Fase | Qué se monta | Costo aprox/mes | Doc |
|---|---|---|---|
| **A** | Backend (ECS), Frontend (ECS), MongoDB Atlas, Redis (ElastiCache), Cognito, VPC, ALB. Workers Temporal quedan locales, expuestos vía ngrok. | **~$80** | [`phase-a.md`](./phase-a.md) |
| **B** | CDC real: MongoDB Atlas → Debezium (Kafka Connect) → Confluent Cloud → S3 sink. | **~$100–200** | `phase-b.md` *(por escribir)* |
| **C** | Databricks Workspace + Unity Catalog + Lakeflow Pipelines (Bronze/Silver/Gold) + Vector Search + Lakehouse Monitoring. | **~$300–500** | `phase-c.md` *(por escribir)* |

**Ya hecho (pre-fase A):**
- `infra/terraform/` con S3 receipts + S3 textract-output + IAM user worker + OIDC role para GitHub Actions.
- `.github/workflows/terraform.yml` con `fmt` + `validate` en PRs.

**Principios del roadmap:**
1. **Cada fase deja algo demoable** (URL compartible, pipeline corriendo, dashboard navegable).
2. **Cada fase es aditiva** — no rompe la anterior. Se puede hacer rollback por fase.
3. **Todo vía Terraform** salvo creaciones one-shot de accounts (AWS, Atlas, Confluent).
4. **Free Tier primero**, upgrades solo cuando se justifican con una razón concreta.
5. **IaC commiteado** al repo para que sea reproducible en una entrevista.

**Orden de implementación:** A → B → C. No saltarse. Cada una tiene como input algo que produjo la anterior (backend escribe Mongo → CDC lo replica → Medallion lo consume).
