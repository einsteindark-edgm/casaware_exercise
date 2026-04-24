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
