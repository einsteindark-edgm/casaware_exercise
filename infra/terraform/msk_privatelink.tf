# ── Phase B++ · PrivateLink frente a MSK Provisioned ─────────────────
#
# Cada broker del MSK Provisioned necesita su propio NLB + VPC Endpoint
# Service. Pattern "NLB per broker" del Databricks Platform SME:
#   https://medium.com/databricks-platform-sme/aws-databricks-serverless-private-connectivity-to-amazon-msk-bbf8cf02efe0
#
# MSK NO publica directamente las IPs de los brokers. Las descubrimos
# via data "aws_network_interfaces" filtrando por la descripción que
# AWS agrega al ENI ("Network interface for Kafka broker <n>...").
# Fallback: var.msk_broker_ips con 2 IPs manuales.
#
# Gateado por var.msk_privatelink_enabled (además de msk_provisioned_enabled).

locals {
  msk_privatelink_active = var.msk_provisioned_enabled && var.msk_privatelink_enabled

  # Si el usuario rellena msk_broker_ips, úsalo. Si no, resolver vía data source.
  msk_broker_ips_effective = (
    length(var.msk_broker_ips) > 0
    ? var.msk_broker_ips
    : (local.msk_privatelink_active
      ? [for eni in data.aws_network_interface.msk_broker : eni.private_ip]
      : []
    )
  )
}

# Descubrimiento de ENIs de brokers. El filtro por descripción es
# best-effort — AWS cambia el string ocasionalmente. Si el data source
# devuelve 0, hay que rellenar var.msk_broker_ips manualmente.
data "aws_network_interfaces" "msk_brokers" {
  count = local.msk_privatelink_active && length(var.msk_broker_ips) == 0 ? 1 : 0

  filter {
    name   = "vpc-id"
    values = [aws_vpc.main.id]
  }

  filter {
    name   = "description"
    values = ["*kafka*${aws_msk_cluster.nexus_prov[0].cluster_name}*"]
  }
}

data "aws_network_interface" "msk_broker" {
  for_each = local.msk_privatelink_active && length(var.msk_broker_ips) == 0 ? toset(data.aws_network_interfaces.msk_brokers[0].ids) : toset([])
  id       = each.value
}

# ── NLB per broker (uno por AZ) ──────────────────────────────────────

resource "aws_lb" "msk_broker" {
  count                            = local.msk_privatelink_active ? length(aws_subnet.private) : 0
  name                             = "${var.prefix}-msk-nlb-${count.index}"
  load_balancer_type               = "network"
  internal                         = true
  subnets                          = [aws_subnet.private[count.index].id]
  enable_cross_zone_load_balancing = false

  tags = { Name = "${var.prefix}-msk-nlb-${count.index}" }
}

resource "aws_lb_target_group" "msk_broker" {
  count       = local.msk_privatelink_active ? length(aws_subnet.private) : 0
  name        = "${var.prefix}-msk-tg-${count.index}"
  port        = 9098
  protocol    = "TCP"
  target_type = "ip"
  vpc_id      = aws_vpc.main.id

  health_check {
    protocol            = "TCP"
    port                = "9098"
    healthy_threshold   = 3
    unhealthy_threshold = 3
    interval            = 30
  }
}

resource "aws_lb_listener" "msk_broker" {
  count             = local.msk_privatelink_active ? length(aws_subnet.private) : 0
  load_balancer_arn = aws_lb.msk_broker[count.index].arn
  port              = 9098
  protocol          = "TCP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.msk_broker[count.index].arn
  }
}

# Attach broker IP al target group. Usamos local.msk_broker_ips_effective;
# si está vacío (primer apply antes de que exista el cluster o el data
# source no matchee), TF crea igual los recursos sin attachments — el
# user corre un segundo apply rellenando var.msk_broker_ips.
resource "aws_lb_target_group_attachment" "msk_broker" {
  count            = local.msk_privatelink_active && length(local.msk_broker_ips_effective) >= length(aws_subnet.private) ? length(aws_subnet.private) : 0
  target_group_arn = aws_lb_target_group.msk_broker[count.index].arn
  target_id        = local.msk_broker_ips_effective[count.index]
  port             = 9098
  # availability_zone solo se setea cuando el target es fuera del VPC.
  # Para IPs intra-VPC, AWS deduce la AZ del IP y rechaza override.
}

# ── VPC Endpoint Service por NLB ─────────────────────────────────────
#
# acceptance_required=false → la conexión desde el NCC de Databricks se
# acepta automáticamente. allowed_principals habilita el account root de
# Databricks Serverless. Ver var.databricks_serverless_principal_arn.

resource "aws_vpc_endpoint_service" "msk_broker" {
  count                      = local.msk_privatelink_active ? length(aws_subnet.private) : 0
  acceptance_required        = false
  network_load_balancer_arns = [aws_lb.msk_broker[count.index].arn]
  # Wildcard temporal para bootstrap del NCC en 2026 — Databricks Serverless
  # rotó el account ID esporadicamente entre 2024-2026. Identificar el
  # principal real via `aws ec2 describe-vpc-endpoint-connections` después
  # de que Databricks NCC cree el endpoint, y restringir a ese ARN específico.
  allowed_principals = ["*"]

  tags = { Name = "${var.prefix}-msk-vpces-${count.index}" }
}

# ── Outputs ──────────────────────────────────────────────────────────

output "msk_broker_ips_discovered" {
  description = "IPs descubiertas via data source. Útil para poblar var.msk_broker_ips si el data source no matchea."
  value       = local.msk_broker_ips_effective
}

output "msk_broker_hostnames" {
  description = "FQDNs de los brokers provisionados. Se registran en NCC como domain_names."
  value = (
    var.msk_provisioned_enabled
    ? [for ep in split(",", aws_msk_cluster.nexus_prov[0].bootstrap_brokers_sasl_iam) : split(":", ep)[0]]
    : []
  )
}

output "msk_endpoint_service_names" {
  description = "Service names de los VPC Endpoint Services. Se usan en databricks_mws_ncc_private_endpoint_rule.endpoint_service."
  value       = local.msk_privatelink_active ? [for s in aws_vpc_endpoint_service.msk_broker : s.service_name] : []
}

output "msk_nlb_arns" {
  description = "ARNs de los NLBs por broker."
  value       = local.msk_privatelink_active ? aws_lb.msk_broker[*].arn : []
}
