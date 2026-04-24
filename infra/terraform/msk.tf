# ── Phase B+ · MSK Serverless (Debezium → Kafka → Databricks) ────────
#
# MSK Serverless cluster usado como transport layer del CDC Mongo→Bronze.
# Debezium Server produce a topics nexus.nexus_dev.<collection> y
# Databricks DLT Structured Streaming consume (source=kafka, IAM auth).
#
# Gateada por var.msk_enabled para poder aplicar el resto del stack
# antes de pagar los ~$150/mo de baseline. Topics se crean manualmente
# desde una task ECS one-shot (ver infra/plans/phase-b-plus-runbook.md).

variable "msk_enabled" {
  description = "Crea el cluster MSK Serverless. Toggle para no pagar baseline hasta el cutover."
  type        = bool
  default     = false
}

# ── Security Group ───────────────────────────────────────────────────
# Permite SASL_SSL IAM (9098) desde Debezium (worker SG reutilizado) y
# desde las NAT IPs del VPC (para que Databricks Serverless llegue por
# peering-free via public bootstrap — MSK Serverless es público con
# autenticación IAM, accesible desde cualquier VPC con credenciales).

resource "aws_security_group" "msk" {
  count       = var.msk_enabled ? 1 : 0
  name        = "${var.prefix}-msk-sg"
  description = "MSK Serverless — IAM SASL 9098 desde debezium ECS + Databricks egress."
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Kafka IAM SASL desde Debezium task"
    from_port       = 9098
    to_port         = 9098
    protocol        = "tcp"
    security_groups = [aws_security_group.worker.id]
  }

  # Databricks Serverless egresses desde sus IPs AWS-internas. Para dev
  # aceptamos ingress 9098 desde el internet público (MSK IAM auth
  # protege el acceso). En prod, restringir a los CIDRs documentados del
  # plano de control de Databricks de la región (us-east-1).
  ingress {
    description = "Kafka IAM SASL public ingress (auth IAM-gated)"
    from_port   = 9098
    to_port     = 9098
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.prefix}-msk-sg" }
}

# ── CloudWatch log group ─────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "msk" {
  count             = var.msk_enabled ? 1 : 0
  name              = "/aws/msk/${var.prefix}"
  retention_in_days = 14
}

# ── Cluster ──────────────────────────────────────────────────────────
# MSK Serverless es multi-AZ por diseño. No hay control de partitions/
# replication-factor a nivel cluster — se setean por topic al crearlos.
# La AWS API actualmente requiere mínimo 2 subnets en AZs distintas.

resource "aws_msk_serverless_cluster" "nexus" {
  count        = var.msk_enabled ? 1 : 0
  cluster_name = "${var.prefix}-kafka"

  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.msk[0].id]
  }

  client_authentication {
    sasl {
      iam {
        enabled = true
      }
    }
  }

  tags = var.tags
}

# ── Cluster resource policy ──────────────────────────────────────────
# Permite que los roles debezium_task y databricks_msk_access se
# autentiquen con IAM SASL. Los kafka-cluster:* permissions que cada
# role necesita están en sus propias policies (ver debezium.tf y
# databricks_msk.tf).

# Note: para MSK Serverless el cluster policy se aplica via
# aws_msk_cluster_policy. Sin embargo, en 2026 AWS permite que roles
# IAM dentro de la misma cuenta invoquen kafka-cluster:* sin cluster
# policy explícita — basta con que el role tenga la action en su
# policy. Dejamos esto documentado por si hay que cross-account.

# ── Outputs ──────────────────────────────────────────────────────────

output "msk_bootstrap_servers" {
  description = "SASL IAM bootstrap brokers (puerto 9098). Vacío si msk_enabled=false."
  value       = var.msk_enabled ? aws_msk_serverless_cluster.nexus[0].bootstrap_brokers_sasl_iam : ""
}

output "msk_cluster_arn" {
  description = "ARN del MSK Serverless cluster. Vacío si msk_enabled=false."
  value       = var.msk_enabled ? aws_msk_serverless_cluster.nexus[0].arn : ""
}
