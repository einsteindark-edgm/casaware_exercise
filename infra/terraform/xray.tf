# ── Phase E.1 — X-Ray sampling rules ─────────────────────────────────
#
# Estrategia: 5% baseline + 5 reservoir/sec, sin filtros por status code
# (no soportado por AWS). El collector ADOT puede agregar tail-sampling
# en cada task para subir la captura de errores si hace falta.

resource "aws_xray_sampling_rule" "default" {
  rule_name      = "nexus-default"
  priority       = 9000
  reservoir_size = 5
  fixed_rate     = 0.05
  service_name   = "*"
  service_type   = "*"
  host           = "*"
  http_method    = "*"
  url_path       = "*"
  resource_arn   = "*"
  version        = 1
}

# Regla específica para POST /v1/expenses y /v1/chat — capturamos 100%
# porque son los puntos de entrada de los flujos largos (HITL, RAG).
resource "aws_xray_sampling_rule" "critical_endpoints" {
  rule_name      = "nexus-critical-eps"
  priority       = 100
  reservoir_size = 2
  fixed_rate     = 1.0
  service_name   = "*"
  service_type   = "*"
  host           = "*"
  http_method    = "POST"
  url_path       = "/v1/expenses*"
  resource_arn   = "*"
  version        = 1
}

resource "aws_xray_sampling_rule" "chat_endpoints" {
  rule_name      = "nexus-chat-eps"
  priority       = 110
  reservoir_size = 2
  fixed_rate     = 1.0
  service_name   = "*"
  service_type   = "*"
  host           = "*"
  http_method    = "*"
  url_path       = "/v1/chat*"
  resource_arn   = "*"
  version        = 1
}
