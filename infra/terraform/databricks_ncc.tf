# ── Phase B++ · Databricks Network Connectivity Configuration ─────────
#
# NCC es account-level. Este archivo usa el provider alias databricks.account.
#
# Resources:
#   - databricks_mws_network_connectivity_config.nexus (1 por workspace/región).
#   - databricks_mws_ncc_private_endpoint_rule.broker[N] (1 por broker).
#
# Attach del NCC al workspace: lo hacemos fuera de Terraform via CLI
# ("databricks account workspaces update") — el workspace fue creado
# manualmente en Phase C.0 y no está importado a TF. Ver runbook.
#
# Gateado por: msk_privatelink_enabled (hay endpoint services disponibles)
# + databricks_account_id no vacío (el SP está configurado).

locals {
  ncc_ready = local.msk_privatelink_active && var.databricks_account_id != "" && var.databricks_client_id != ""
}

resource "databricks_mws_network_connectivity_config" "nexus" {
  provider = databricks.account
  count    = local.ncc_ready ? 1 : 0

  name   = "${var.prefix}-ncc"
  region = var.aws_region
}

# Una rule por broker. Cada rule asocia:
#   - el endpoint_service del broker (VPC Endpoint Service name)
#   - el domain name que el cliente Kafka verá como bootstrap/broker
#
# Databricks provider (>=1.48) puede exponer este resource como
# databricks_mws_ncc_private_endpoint_rule con distintos schemas:
#   - resource_names (flag AWS PrivateLink interface endpoint)
#   - endpoint_service o resource_id (el VPCES name)
#   - domain_names (FQDNs que deben resolverse al endpoint)
# Si el apply falla por schema mismatch, revisar CHANGELOG del provider.

resource "databricks_mws_ncc_private_endpoint_rule" "msk_broker" {
  provider = databricks.account
  count    = local.ncc_ready ? length(aws_vpc_endpoint_service.msk_broker) : 0

  network_connectivity_config_id = databricks_mws_network_connectivity_config.nexus[0].network_connectivity_config_id
  resource_names                 = [aws_vpc_endpoint_service.msk_broker[count.index].service_name]
  group_id                       = "com.amazonaws.vpce.${var.aws_region}" # placeholder si el provider lo requiere
  domain_names                   = [element([for ep in split(",", aws_msk_cluster.nexus_prov[0].bootstrap_brokers_sasl_iam) : split(":", ep)[0]], count.index)]
}

output "databricks_ncc_id" {
  description = "ID del NCC para el attach manual al workspace."
  value       = local.ncc_ready ? databricks_mws_network_connectivity_config.nexus[0].network_connectivity_config_id : ""
  sensitive   = true
}

output "databricks_ncc_attach_cli_hint" {
  description = "Comando CLI para adjuntar el NCC al workspace (ejecutar como account admin)."
  value = local.ncc_ready && var.databricks_workspace_id != "" ? (
    "databricks account workspaces update ${var.databricks_workspace_id} --network-connectivity-config-id ${databricks_mws_network_connectivity_config.nexus[0].network_connectivity_config_id}"
  ) : ""
  sensitive = true
}
