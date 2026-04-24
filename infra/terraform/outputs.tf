output "aws_account_id" {
  description = "Account ID where these resources live."
  value       = data.aws_caller_identity.current.account_id
}

# ── S3 + IAM worker (pre-Phase A) ────────────────────────────────────

output "receipts_bucket" {
  description = "Name of the S3 bucket that stores uploaded receipts."
  value       = aws_s3_bucket.receipts.bucket
}

output "textract_output_bucket" {
  description = "Name of the S3 bucket where the worker persists raw Textract JSON for lineage."
  value       = aws_s3_bucket.textract_output.bucket
}

output "worker_iam_user" {
  description = "IAM user name used by the local backend + worker containers."
  value       = aws_iam_user.worker.name
}

output "worker_access_key_id" {
  description = "Access key id for the worker IAM user — put this in backend/.env and nexus-orchestration/.env."
  value       = aws_iam_access_key.worker.id
  sensitive   = true
}

output "worker_secret_access_key" {
  description = "Secret key for the worker IAM user — put this in backend/.env and nexus-orchestration/.env."
  value       = aws_iam_access_key.worker.secret
  sensitive   = true
}

output "gha_terraform_role_arn" {
  description = "IAM role ARN that GitHub Actions assumes via OIDC."
  value       = aws_iam_role.gha_terraform.arn
}

# ── Networking (Phase A.1) ───────────────────────────────────────────

output "vpc_id" {
  value = aws_vpc.main.id
}

output "public_subnet_ids" {
  value = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "nat_public_ip" {
  description = "Elastic IP del NAT — usar para whitelisting en MongoDB Atlas."
  value       = aws_eip.nat.public_ip
}

# ── Cognito (Phase A.2) ──────────────────────────────────────────────

output "cognito_user_pool_id" {
  value = aws_cognito_user_pool.main.id
}

output "cognito_client_id" {
  value = aws_cognito_user_pool_client.web.id
}

output "cognito_hosted_ui_domain" {
  value = "${aws_cognito_user_pool_domain.main.domain}.auth.${var.aws_region}.amazoncognito.com"
}

output "cognito_jwks_url" {
  value = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.main.id}/.well-known/jwks.json"
}

# ── ECR (Phase A.5) ──────────────────────────────────────────────────

output "ecr_backend_repo_url" {
  value = aws_ecr_repository.backend.repository_url
}

output "ecr_frontend_repo_url" {
  value = aws_ecr_repository.frontend.repository_url
}

# ── ElastiCache (Phase A.4) ──────────────────────────────────────────

output "redis_endpoint" {
  value = aws_elasticache_cluster.redis.cache_nodes[0].address
}

output "redis_port" {
  value = aws_elasticache_cluster.redis.cache_nodes[0].port
}

# ── ALB (Phase A.7) ──────────────────────────────────────────────────

output "alb_dns_name" {
  value = aws_lb.main.dns_name
}

output "alb_zone_id" {
  value = aws_lb.main.zone_id
}

# ── ECS (Phase A.8) ──────────────────────────────────────────────────

output "ecs_cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "ecs_backend_service_name" {
  value = aws_ecs_service.backend.name
}

output "ecs_frontend_service_name" {
  value = aws_ecs_service.frontend.name
}

# ── Databricks / Unity Catalog (Phase C) ─────────────────────────────

output "databricks_uc_bucket" {
  description = "S3 bucket usado como managed storage del catalog nexus_dev. Vacio hasta C.2."
  value       = var.databricks_enabled ? aws_s3_bucket.uc_root[0].bucket : ""
}

output "databricks_uc_role_arn" {
  description = "IAM role ARN que UC asume. Pegalo en el storage credential si lo creas via UI."
  value       = var.databricks_enabled ? aws_iam_role.uc_access[0].arn : ""
}

output "databricks_catalog" {
  description = "Nombre del catalog Unity. Se usa en las vars del worker y del asset bundle."
  value       = var.databricks_enabled ? databricks_catalog.nexus_dev[0].name : ""
}

data "aws_caller_identity" "current" {}
