output "aws_account_id" {
  description = "Account ID where these resources live."
  value       = data.aws_caller_identity.current.account_id
}

output "receipts_bucket" {
  description = "Name of the S3 bucket that stores uploaded receipts."
  value       = aws_s3_bucket.receipts.bucket
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

data "aws_caller_identity" "current" {}
