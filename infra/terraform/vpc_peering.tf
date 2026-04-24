# ── Phase B+++ · VPC Peering Databricks ↔ MSK + Route53 Resolver ──────
#
# DLT Serverless no soporta NCC PrivateLink (gated por Databricks support
# ticket). Pivote: DLT Classic compute corre en la VPC del workspace
# (customer-managed, 10.227.0.0/16) y se peerea con la VPC propia
# (10.0.0.0/16) que aloja MSK Provisioned.
#
# Adicional: Route53 Resolver para que los FQDNs de brokers MSK
# (b-N.<cluster>.<uuid>.c2.kafka.us-east-1.amazonaws.com) resuelvan
# desde la VPC del workspace. AWS-managed private hosted zone de MSK
# no es asociable a peer VPC vía aws_route53_zone_association — es
# AWS-internal. Forwarding rules vía Resolver es el patrón soportado.

variable "workspace_vpc_id" {
  description = "VPC del workspace Databricks (customer-managed, mismo account)."
  type        = string
  default     = "vpc-08a19874df98c70bc"
}

variable "workspace_vpc_cidr" {
  description = "CIDR de la VPC del workspace Databricks."
  type        = string
  default     = "10.227.0.0/16"
}

variable "workspace_subnet_ids_for_resolver" {
  description = "2 subnets privadas del workspace para outbound Route53 Resolver endpoint (AZs distintas)."
  type        = list(string)
  default     = ["subnet-0d815a289730b7c95", "subnet-031852273277943a0"]
}

variable "workspace_route_table_id" {
  description = "Route table principal de la VPC del workspace (la que tiene asociadas las private subnets)."
  type        = string
  default     = "rtb-0142a15c81deb656e"
}

variable "vpc_peering_enabled" {
  description = "Crea peering connection + routes + Route53 Resolver. Plan B+++ (DLT classic)."
  type        = bool
  default     = false
}

# ── VPC Peering Connection ────────────────────────────────────────────

resource "aws_vpc_peering_connection" "databricks_msk" {
  count       = var.vpc_peering_enabled ? 1 : 0
  vpc_id      = aws_vpc.main.id      # MSK VPC (10.0.0.0/16)
  peer_vpc_id = var.workspace_vpc_id # Databricks workspace VPC (10.227.0.0/16)
  auto_accept = true                 # Same account, auto-accept

  tags = {
    Name = "${var.prefix}-databricks-msk"
    Side = "requester"
  }
}

# allow_remote_vpc_dns_resolution debe activarse en AMBOS lados.
# Para que un peer VPC resuelva DNS público a IPs privadas del otro.
# (Necesario aunque el verdadero problema es el private hosted zone de
# MSK — eso lo resuelve Route53 Resolver, no este flag. Pero AWS lo
# requiere igual para que cualquier query DNS atraviese el peer.)
resource "aws_vpc_peering_connection_options" "databricks_msk_requester" {
  count                     = var.vpc_peering_enabled ? 1 : 0
  vpc_peering_connection_id = aws_vpc_peering_connection.databricks_msk[0].id
  requester {
    allow_remote_vpc_dns_resolution = true
  }
}

resource "aws_vpc_peering_connection_options" "databricks_msk_accepter" {
  count                     = var.vpc_peering_enabled ? 1 : 0
  vpc_peering_connection_id = aws_vpc_peering_connection.databricks_msk[0].id
  accepter {
    allow_remote_vpc_dns_resolution = true
  }
}

# ── Routes en MSK VPC: 10.227.0.0/16 → peer ──────────────────────────

resource "aws_route" "msk_to_databricks_private" {
  count                     = var.vpc_peering_enabled ? 1 : 0
  route_table_id            = aws_route_table.private.id
  destination_cidr_block    = var.workspace_vpc_cidr
  vpc_peering_connection_id = aws_vpc_peering_connection.databricks_msk[0].id
}

# ── Routes en Databricks VPC: 10.0.0.0/16 → peer ──────────────────────
# La VPC del workspace fue creada por Databricks; sus route tables están
# fuera del state de TF pero AWS permite añadirles routes.

resource "aws_route" "databricks_to_msk" {
  count                     = var.vpc_peering_enabled ? 1 : 0
  route_table_id            = var.workspace_route_table_id
  destination_cidr_block    = aws_vpc.main.cidr_block # 10.0.0.0/16
  vpc_peering_connection_id = aws_vpc_peering_connection.databricks_msk[0].id
}

# ── MSK SG: ingress 9098 desde 10.227.0.0/16 ─────────────────────────

resource "aws_security_group_rule" "msk_prov_ingress_databricks" {
  count             = var.vpc_peering_enabled && var.msk_provisioned_enabled ? 1 : 0
  type              = "ingress"
  from_port         = 9098
  to_port           = 9098
  protocol          = "tcp"
  security_group_id = aws_security_group.msk_prov[0].id
  cidr_blocks       = [var.workspace_vpc_cidr]
  description       = "Kafka IAM SASL desde Databricks workspace VPC (peering)"
}

# ── Route53 Resolver: inbound (MSK VPC) + outbound (Databricks VPC) ──
#
# Pattern: clusters DLT Classic en Databricks VPC consultan Route53
# Resolver outbound endpoint → reenvía a inbound endpoint en MSK VPC
# → resuelve la zona privada AWS-managed *.kafka.us-east-1.amazonaws.com
# que solo es accesible desde la VPC owner de MSK.

# SG para inbound resolver endpoint en MSK VPC.
resource "aws_security_group" "resolver_inbound" {
  count       = var.vpc_peering_enabled ? 1 : 0
  name        = "${var.prefix}-resolver-inbound-sg"
  description = "Route53 Resolver inbound endpoint - DNS 53 desde Databricks VPC."
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "DNS UDP desde Databricks VPC"
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = [var.workspace_vpc_cidr]
  }

  ingress {
    description = "DNS TCP desde Databricks VPC"
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = [var.workspace_vpc_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.prefix}-resolver-inbound-sg" }
}

resource "aws_route53_resolver_endpoint" "inbound" {
  count              = var.vpc_peering_enabled ? 1 : 0
  name               = "${var.prefix}-resolver-inbound"
  direction          = "INBOUND"
  security_group_ids = [aws_security_group.resolver_inbound[0].id]

  dynamic "ip_address" {
    for_each = aws_subnet.private[*].id
    content {
      subnet_id = ip_address.value
    }
  }

  tags = { Name = "${var.prefix}-resolver-inbound" }
}

# SG para outbound resolver endpoint en Databricks VPC.
resource "aws_security_group" "resolver_outbound" {
  count       = var.vpc_peering_enabled ? 1 : 0
  name        = "${var.prefix}-resolver-outbound-sg"
  description = "Route53 Resolver outbound endpoint - DNS 53 hacia MSK VPC."
  vpc_id      = var.workspace_vpc_id

  ingress {
    description = "DNS UDP intra-VPC (Databricks compute)"
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = [var.workspace_vpc_cidr]
  }

  ingress {
    description = "DNS TCP intra-VPC"
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = [var.workspace_vpc_cidr]
  }

  egress {
    description = "DNS UDP hacia inbound resolver en MSK VPC"
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }

  egress {
    description = "DNS TCP hacia inbound resolver"
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }

  tags = { Name = "${var.prefix}-resolver-outbound-sg" }
}

resource "aws_route53_resolver_endpoint" "outbound" {
  count              = var.vpc_peering_enabled ? 1 : 0
  name               = "${var.prefix}-resolver-outbound"
  direction          = "OUTBOUND"
  security_group_ids = [aws_security_group.resolver_outbound[0].id]

  dynamic "ip_address" {
    for_each = var.workspace_subnet_ids_for_resolver
    content {
      subnet_id = ip_address.value
    }
  }

  tags = { Name = "${var.prefix}-resolver-outbound" }
}

# Forwarding rule: queries para *.kafka.us-east-1.amazonaws.com →
# inbound endpoint del MSK VPC (que las resolverá vía la zona privada
# AWS-managed que solo el owner VPC ve).
resource "aws_route53_resolver_rule" "msk_kafka" {
  count                = var.vpc_peering_enabled ? 1 : 0
  name                 = "${var.prefix}-resolver-rule-msk"
  domain_name          = "kafka.us-east-1.amazonaws.com"
  rule_type            = "FORWARD"
  resolver_endpoint_id = aws_route53_resolver_endpoint.outbound[0].id

  dynamic "target_ip" {
    for_each = aws_route53_resolver_endpoint.inbound[0].ip_address
    content {
      ip = target_ip.value.ip
    }
  }

  tags = { Name = "${var.prefix}-resolver-rule-msk" }
}

# Asociar la rule con la VPC del workspace para que efectivamente la use.
resource "aws_route53_resolver_rule_association" "msk_kafka_databricks" {
  count            = var.vpc_peering_enabled ? 1 : 0
  resolver_rule_id = aws_route53_resolver_rule.msk_kafka[0].id
  vpc_id           = var.workspace_vpc_id
}

# ── Outputs ──────────────────────────────────────────────────────────

output "vpc_peering_connection_id" {
  description = "Peering connection ID Databricks ↔ MSK."
  value       = var.vpc_peering_enabled ? aws_vpc_peering_connection.databricks_msk[0].id : ""
}

output "resolver_inbound_ips" {
  description = "IPs del Route53 Resolver inbound endpoint (target del forwarding rule)."
  value       = var.vpc_peering_enabled ? [for ip in aws_route53_resolver_endpoint.inbound[0].ip_address : ip.ip] : []
}
