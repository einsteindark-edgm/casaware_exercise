# ── Cognito User Pool (Phase A.2) ────────────────────────────────────
#
# User pool + app client + hosted UI domain. El backend lee el JWT y
# valida contra JWKS. `custom:tenant_id` se asigna manualmente en
# Phase A via `aws cognito-idp admin-update-user-attributes`.
#
# Nota: las callback URLs se fijan al ALB DNS. Eso genera un ciclo
# (Cognito↔ALB). Resolvemos con var `cognito_callback_domain`: primer
# apply con "" (sin callbacks válidos), segundo apply tras Phase A.7
# con el DNS real del ALB.

variable "cognito_callback_domain" {
  description = "Domain where the frontend lives (e.g. nexus.example.com o el DNS del ALB). Vacío en el primer apply."
  type        = string
  default     = ""
}

resource "aws_cognito_user_pool" "main" {
  name = "${var.prefix}-users"

  password_policy {
    minimum_length                   = 8
    require_lowercase                = true
    require_numbers                  = true
    require_symbols                  = false
    require_uppercase                = true
    temporary_password_validity_days = 7
  }

  auto_verified_attributes = ["email"]
  username_attributes      = ["email"]

  admin_create_user_config {
    allow_admin_create_user_only = false
  }

  schema {
    name                = "tenant_id"
    attribute_data_type = "String"
    mutable             = true
    required            = false

    string_attribute_constraints {
      min_length = 1
      max_length = 64
    }
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  tags = { Name = "${var.prefix}-users" }
}

resource "aws_cognito_user_pool_client" "web" {
  name         = "${var.prefix}-web"
  user_pool_id = aws_cognito_user_pool.main.id

  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "profile", "email"]
  supported_identity_providers         = ["COGNITO"]

  # ALB sólo expone HTTP (sin dominio + ACM). Cognito Hosted UI acepta HTTP
  # únicamente para callbacks a "localhost". Para el DNS del ALB debe ser HTTPS,
  # así que cuando no hay dominio dejamos un placeholder "valid-but-unused" y
  # usas localhost:3000 mientras pruebas en dev. En prod con dominio, cambia
  # var.cognito_callback_domain al hostname real.
  callback_urls = var.cognito_callback_domain == "" ? [
    "http://localhost:3000/auth/callback",
    "http://localhost:3000/",
    ] : [
    "https://${var.cognito_callback_domain}/auth/callback",
    "https://${var.cognito_callback_domain}/",
  ]
  logout_urls = var.cognito_callback_domain == "" ? [
    "http://localhost:3000/login",
    "http://localhost:3000/",
    ] : [
    "https://${var.cognito_callback_domain}/login",
    "https://${var.cognito_callback_domain}/",
  ]

  explicit_auth_flows = [
    "ALLOW_USER_SRP_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
    "ALLOW_ADMIN_USER_PASSWORD_AUTH",
  ]

  access_token_validity  = 1
  id_token_validity      = 1
  refresh_token_validity = 30
  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }

  prevent_user_existence_errors = "ENABLED"
}

resource "aws_cognito_user_pool_domain" "main" {
  domain       = "${var.prefix}-auth"
  user_pool_id = aws_cognito_user_pool.main.id
}
