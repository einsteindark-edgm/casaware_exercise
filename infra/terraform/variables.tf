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
