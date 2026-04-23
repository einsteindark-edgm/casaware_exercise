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
