# ── Secrets Manager + SSM (Phase A.6) ────────────────────────────────
#
# Secretos: valores sensibles (Mongo URI, tunnel de Temporal).
# SSM plaintext params: ids de Cognito, nombres de buckets, etc.
#
# Valores placeholder iniciales; luego se sobrescriben con
#   aws secretsmanager put-secret-value --secret-id ... --secret-string '...'

resource "aws_secretsmanager_secret" "mongodb_uri" {
  name                    = "${var.prefix}/mongodb_uri"
  description             = "MongoDB Atlas connection string (mongodb+srv://...)."
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "mongodb_uri_initial" {
  secret_id     = aws_secretsmanager_secret.mongodb_uri.id
  secret_string = "REPLACE_ME_AFTER_ATLAS_SETUP"

  lifecycle {
    ignore_changes = [secret_string]
  }
}

resource "aws_secretsmanager_secret" "temporal_ngrok_url" {
  name                    = "${var.prefix}/temporal_ngrok_url"
  description             = "TCP endpoint for the local Temporal server exposed via ngrok (e.g. 4.tcp.ngrok.io:12345)."
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "temporal_ngrok_url_initial" {
  secret_id     = aws_secretsmanager_secret.temporal_ngrok_url.id
  secret_string = "REPLACE_ME_AFTER_NGROK_UP"

  lifecycle {
    ignore_changes = [secret_string]
  }
}

resource "aws_secretsmanager_secret" "redis_url" {
  name                    = "${var.prefix}/redis_url"
  description             = "redis:// URL built from the ElastiCache primary endpoint."
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "redis_url" {
  secret_id     = aws_secretsmanager_secret.redis_url.id
  secret_string = "redis://${aws_elasticache_cluster.redis.cache_nodes[0].address}:${aws_elasticache_cluster.redis.cache_nodes[0].port}/0"
}

# SSM plaintext params (gratis).
resource "aws_ssm_parameter" "aws_region" {
  name  = "/${var.prefix}/aws_region"
  type  = "String"
  value = var.aws_region
}

resource "aws_ssm_parameter" "s3_receipts_bucket" {
  name  = "/${var.prefix}/s3_receipts_bucket"
  type  = "String"
  value = aws_s3_bucket.receipts.bucket
}

resource "aws_ssm_parameter" "s3_textract_output_bucket" {
  name  = "/${var.prefix}/s3_textract_output_bucket"
  type  = "String"
  value = aws_s3_bucket.textract_output.bucket
}

resource "aws_ssm_parameter" "cognito_user_pool_id" {
  name  = "/${var.prefix}/cognito_user_pool_id"
  type  = "String"
  value = aws_cognito_user_pool.main.id
}

resource "aws_ssm_parameter" "cognito_client_id" {
  name  = "/${var.prefix}/cognito_client_id"
  type  = "String"
  value = aws_cognito_user_pool_client.web.id
}

resource "aws_ssm_parameter" "cognito_jwks_url" {
  name  = "/${var.prefix}/cognito_jwks_url"
  type  = "String"
  value = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.main.id}/.well-known/jwks.json"
}

# ── Databricks (Phase C) ─────────────────────────────────────────────
# Secretos sensibles (host y token del workspace). Poblalos con:
#   aws secretsmanager put-secret-value --secret-id nexus-dev-edgm/databricks_host ...
#   aws secretsmanager put-secret-value --secret-id nexus-dev-edgm/databricks_token ...
# Se crean siempre (aunque databricks_enabled=false) para que el
# task definition del worker pueda referenciarlos sin drift.

resource "aws_secretsmanager_secret" "databricks_host" {
  name                    = "${var.prefix}/databricks_host"
  description             = "Databricks workspace URL (https://dbc-...cloud.databricks.com)."
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "databricks_host_initial" {
  secret_id     = aws_secretsmanager_secret.databricks_host.id
  secret_string = "REPLACE_ME_AFTER_PHASE_C0"

  lifecycle {
    ignore_changes = [secret_string]
  }
}

resource "aws_secretsmanager_secret" "databricks_token" {
  name                    = "${var.prefix}/databricks_token"
  description             = "Databricks PAT for the worker to call Vector Search."
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "databricks_token_initial" {
  secret_id     = aws_secretsmanager_secret.databricks_token.id
  secret_string = "REPLACE_ME_AFTER_PHASE_C0"

  lifecycle {
    ignore_changes = [secret_string]
  }
}

resource "aws_ssm_parameter" "databricks_vs_endpoint" {
  name  = "/${var.prefix}/databricks_vs_endpoint"
  type  = "String"
  value = "nexus-vs-dev"
}

resource "aws_ssm_parameter" "databricks_vs_index" {
  name  = "/${var.prefix}/databricks_vs_index"
  type  = "String"
  value = "${var.databricks_catalog_name}.vector.expense_chunks_index"
}

# Phase D: warehouse id needed by `search_expenses_structured` activity to
# run parametrised SQL against gold.expense_audit. Stored as a secret because
# anyone with the id + token can query the warehouse.
resource "aws_secretsmanager_secret" "databricks_warehouse_id" {
  name                    = "${var.prefix}/databricks_warehouse_id"
  description             = "Databricks SQL warehouse id used by the RAG SQL tool."
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "databricks_warehouse_id_initial" {
  secret_id     = aws_secretsmanager_secret.databricks_warehouse_id.id
  secret_string = "REPLACE_ME_AFTER_PHASE_D"

  lifecycle {
    ignore_changes = [secret_string]
  }
}
