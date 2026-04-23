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
