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
  description = "MSK Serverless - IAM SASL 9098 from debezium ECS + Databricks egress."
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
  description = "SASL IAM bootstrap brokers del cluster ACTIVO (Provisioned si msk_provisioned_enabled=true, si no Serverless). Vacío si ambos están off."
  value       = local.msk_bootstrap_active
}

output "msk_cluster_arn" {
  description = "ARN del cluster MSK ACTIVO."
  value       = local.msk_arn_active
}

# ── Phase B++ · MSK Provisioned (paralelo al Serverless durante cutover) ──
#
# Se crea al lado del Serverless (ambos activos durante la migración).
# Una vez validado Debezium + Databricks apuntando al Provisioned, se
# destruye el Serverless borrando var.msk_enabled=false.
#
# locals.msk_arn_active y locals.msk_bootstrap_active son los que usan
# los downstream (debezium.tf, databricks_msk.tf, bundle databricks.yml).
# Por ahora prefieren Serverless para no romper nada hasta el cutover.
# En el step de cutover se flipea el default a "prefer_provisioned".

locals {
  msk_prefer_provisioned = var.msk_provisioned_enabled && var.msk_prefer_provisioned

  msk_arn_active = (
    local.msk_prefer_provisioned ? aws_msk_cluster.nexus_prov[0].arn :
    var.msk_enabled ? aws_msk_serverless_cluster.nexus[0].arn : ""
  )

  msk_bootstrap_active = (
    local.msk_prefer_provisioned ? aws_msk_cluster.nexus_prov[0].bootstrap_brokers_sasl_iam :
    var.msk_enabled ? aws_msk_serverless_cluster.nexus[0].bootstrap_brokers_sasl_iam : ""
  )
}

# Security group para el MSK Provisioned cluster. Separado del SG del
# Serverless para no alterar las reglas del cluster activo durante la
# migración. Ingress 9098 desde worker SG (Debezium) + self (broker↔
# broker) + desde el SG del NLB (privatelink).

resource "aws_security_group" "msk_prov" {
  count       = var.msk_provisioned_enabled ? 1 : 0
  name        = "${var.prefix}-msk-prov-sg"
  description = "MSK Provisioned brokers - IAM SASL 9098 intra-VPC only."
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${var.prefix}-msk-prov-sg" }
}

resource "aws_security_group_rule" "msk_prov_ingress_debezium" {
  count                    = var.msk_provisioned_enabled ? 1 : 0
  type                     = "ingress"
  from_port                = 9098
  to_port                  = 9098
  protocol                 = "tcp"
  security_group_id        = aws_security_group.msk_prov[0].id
  source_security_group_id = aws_security_group.worker.id
  description              = "Kafka IAM SASL desde Debezium task"
}

resource "aws_security_group_rule" "msk_prov_ingress_self" {
  count                    = var.msk_provisioned_enabled ? 1 : 0
  type                     = "ingress"
  from_port                = 0
  to_port                  = 65535
  protocol                 = "tcp"
  security_group_id        = aws_security_group.msk_prov[0].id
  source_security_group_id = aws_security_group.msk_prov[0].id
  description              = "Broker to broker traffic"
}

resource "aws_security_group_rule" "msk_prov_ingress_nlb" {
  count             = var.msk_provisioned_enabled && var.msk_privatelink_enabled ? 1 : 0
  type              = "ingress"
  from_port         = 9098
  to_port           = 9098
  protocol          = "tcp"
  security_group_id = aws_security_group.msk_prov[0].id
  cidr_blocks       = [aws_vpc.main.cidr_block]
  description       = "Kafka IAM SASL desde NLB (PrivateLink target)"
}

resource "aws_security_group_rule" "msk_prov_egress_all" {
  count             = var.msk_provisioned_enabled ? 1 : 0
  type              = "egress"
  from_port         = 0
  to_port           = 0
  protocol          = "-1"
  security_group_id = aws_security_group.msk_prov[0].id
  cidr_blocks       = ["0.0.0.0/0"]
  description       = "Egress all"
}

# Configuration del cluster. MSK Provisioned permite overrides que
# Serverless no permite (p.ej. advertised.listeners se fija por AWS
# según el connectivity_info; no lo tocamos acá).
resource "aws_msk_configuration" "nexus_prov" {
  count             = var.msk_provisioned_enabled ? 1 : 0
  name              = "${var.prefix}-kafka-config"
  kafka_versions    = [var.msk_kafka_version]
  server_properties = <<-EOT
    auto.create.topics.enable=false
    default.replication.factor=2
    min.insync.replicas=2
    num.partitions=3
    log.retention.hours=168
  EOT
}

resource "aws_msk_cluster" "nexus_prov" {
  count                  = var.msk_provisioned_enabled ? 1 : 0
  cluster_name           = "${var.prefix}-kafka-prov"
  kafka_version          = var.msk_kafka_version
  number_of_broker_nodes = length(aws_subnet.private)

  broker_node_group_info {
    instance_type   = var.msk_broker_instance_type
    client_subnets  = aws_subnet.private[*].id
    security_groups = [aws_security_group.msk_prov[0].id]
    storage_info {
      ebs_storage_info {
        volume_size = var.msk_broker_volume_gb
      }
    }
    connectivity_info {
      public_access {
        type = "DISABLED"
      }
    }
  }

  client_authentication {
    sasl {
      iam = true
    }
  }

  encryption_info {
    encryption_in_transit {
      client_broker = "TLS"
      in_cluster    = true
    }
  }

  configuration_info {
    arn      = aws_msk_configuration.nexus_prov[0].arn
    revision = aws_msk_configuration.nexus_prov[0].latest_revision
  }

  logging_info {
    broker_logs {
      cloudwatch_logs {
        enabled   = true
        log_group = aws_cloudwatch_log_group.msk[0].name
      }
    }
  }

  tags = var.tags
}

output "msk_prov_bootstrap_servers" {
  description = "Bootstrap del MSK Provisioned (independiente del cluster activo)."
  value       = var.msk_provisioned_enabled ? aws_msk_cluster.nexus_prov[0].bootstrap_brokers_sasl_iam : ""
}

output "msk_prov_cluster_arn" {
  description = "ARN del MSK Provisioned."
  value       = var.msk_provisioned_enabled ? aws_msk_cluster.nexus_prov[0].arn : ""
}

output "msk_prov_cluster_name" {
  description = "Nombre del cluster Provisioned (para filtros de ENI en privatelink)."
  value       = var.msk_provisioned_enabled ? aws_msk_cluster.nexus_prov[0].cluster_name : ""
}
