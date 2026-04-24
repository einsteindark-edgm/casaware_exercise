variable "aws_region" {
  description = "AWS region for all resources."
  type        = string
  default     = "us-east-1"
}

variable "prefix" {
  description = "Prefix applied to every resource name. Keep short and unique."
  type        = string
  default     = "nexus-dev-edgm"
}

variable "github_repo" {
  description = "GitHub repository in owner/repo form, used in the OIDC trust policy."
  type        = string
  default     = "einsteindark-edgm/casaware_exercise"
}

variable "tags" {
  description = "Default tags attached to every resource."
  type        = map(string)
  default = {
    Project   = "nexus"
    Env       = "dev"
    Owner     = "edgm"
    ManagedBy = "terraform"
  }
}

# ── Phase C · Databricks ─────────────────────────────────────────────
# Estos valores se llenan tras completar el runbook C.0 manual
# (signup + workspace + metastore + PAT) y se pegan en terraform.tfvars.
# Si estan vacios, los recursos databricks_* NO se crean: el bloque
# count = var.databricks_enabled ? 1 : 0 controla la activacion.

variable "databricks_enabled" {
  description = "Flip to true after Phase C.0 (workspace + metastore + PAT listos)."
  type        = bool
  default     = false
}

variable "databricks_host" {
  description = "Workspace URL, ej: https://dbc-xxxxxxxx-xxxx.cloud.databricks.com"
  type        = string
  default     = ""
}

variable "databricks_token" {
  description = "Personal Access Token del workspace (sensitive, no commitear)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "databricks_metastore_id" {
  description = "Unity Catalog metastore ID (visible en account console -> Metastores)."
  type        = string
  default     = ""
}

variable "databricks_uc_external_id" {
  description = "External ID que UC muestra en el modal 'Create metastore' -> se usa en el trust policy del IAM role para cross-account assume. Distinto del metastore_id."
  type        = string
  default     = ""
}

variable "databricks_catalog_name" {
  description = "Unity Catalog name. Sigue el prefix de AWS para consistencia."
  type        = string
  default     = "nexus_dev"
}

# ── Phase B++ · MSK Provisioned + PrivateLink + NCC ──────────────────
# MSK Serverless no soporta PrivateLink desde Databricks Serverless
# compute (confirmado por Databricks SME). Migramos a MSK Provisioned
# con NLB-per-broker + VPC Endpoint Services + Databricks NCC. Ver
# /Users/edgm/.claude/plans/groovy-herding-aho.md.
#
# Estos flags permiten montar el stack nuevo al lado del Serverless
# durante el cutover. Serverless se destruye solo después de validar
# Provisioned + NCC end-to-end.

variable "msk_provisioned_enabled" {
  description = "Crea el cluster MSK Provisioned en paralelo al Serverless. Gate del Phase B++."
  type        = bool
  default     = false
}

variable "msk_broker_instance_type" {
  description = "Tipo de broker MSK Provisioned. t3.small para dev."
  type        = string
  default     = "kafka.t3.small"
}

variable "msk_broker_volume_gb" {
  description = "Volumen EBS gp3 por broker (GB)."
  type        = number
  default     = 100
}

variable "msk_kafka_version" {
  description = "Versión de Kafka para el cluster Provisioned. AWS acepta formato x.y.x (wildcard) o exacto."
  type        = string
  default     = "3.8.x"
}

variable "msk_privatelink_enabled" {
  description = "Crea NLBs + VPC Endpoint Services frente a cada broker provisionado. Requiere msk_provisioned_enabled=true."
  type        = bool
  default     = false
}

variable "msk_prefer_provisioned" {
  description = "Cutover flag: cuando es true, Debezium y Databricks apuntan al cluster Provisioned (no al Serverless). Flipear solo después de validar Provisioned + NCC."
  type        = bool
  default     = false
}

variable "msk_broker_ips" {
  description = "Fallback: IPs privadas de los brokers MSK Provisioned. Si está vacío, TF las descubre via data aws_network_interfaces. Rellenar si el data source no matchea."
  type        = list(string)
  default     = []
}

variable "databricks_account_id" {
  description = "Databricks account ID (UUID), visible en accounts.cloud.databricks.com. Requerido para NCC."
  type        = string
  default     = ""
}

variable "databricks_client_id" {
  description = "Client ID del Service Principal de account admin. Para el provider databricks.account."
  type        = string
  default     = ""
  sensitive   = true
}

variable "databricks_client_secret" {
  description = "Client secret del SP. Sensitive."
  type        = string
  default     = ""
  sensitive   = true
}

variable "databricks_serverless_principal_arn" {
  description = "IAM principal ARN del Databricks Serverless compute plane en la región. us-east-1 default = 790110701330. Validar contra doc oficial antes del apply."
  type        = string
  default     = "arn:aws:iam::790110701330:root"
}

variable "databricks_workspace_id" {
  description = "Workspace ID numérico (visible en el URL del workspace: dbc-XXXX.cloud.databricks.com?o=<workspace_id>). Requerido para attach del NCC al workspace."
  type        = string
  default     = ""
}
