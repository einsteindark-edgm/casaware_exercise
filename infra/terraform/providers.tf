provider "aws" {
  region = var.aws_region

  default_tags {
    tags = var.tags
  }
}

# Databricks provider: host + token vienen del runbook C.0. Cuando
# databricks_enabled=false, los recursos *.databricks.* no se crean,
# pero el provider igual debe estar declarado (con valores vacios
# Terraform no intenta llamar al API). Si viendo esto despues de C.0,
# setea las vars en terraform.tfvars.
provider "databricks" {
  host  = var.databricks_host
  token = var.databricks_token
}

# Databricks ACCOUNT-level provider para NCC (recursos databricks_mws_*).
# NCC vive en el plano de control del account, no del workspace.
# Requiere un Service Principal creado en accounts.cloud.databricks.com
# con rol de account admin, y client_id + client_secret en tfvars.
provider "databricks" {
  alias         = "account"
  host          = "https://accounts.cloud.databricks.com"
  account_id    = var.databricks_account_id
  client_id     = var.databricks_client_id
  client_secret = var.databricks_client_secret
}
