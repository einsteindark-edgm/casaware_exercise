# ── ACM cert (Phase A.7, opcional) ───────────────────────────────────
#
# Sólo se crea si se provee var.domain_name. DNS validation requiere
# el hosted zone también; si el dominio está en Route 53 en esta
# cuenta se puede automatizar. Si el dominio vive fuera (p.e.
# Cloudflare), crear los records CNAME a mano.
#
# Para arrancar sin dominio: dejar domain_name = "" (default) y usar
# el DNS del ALB por HTTP.

variable "domain_name" {
  description = "Dominio a mapear al ALB (e.g. nexus.miapp.com). Vacío para usar el DNS del ALB directamente."
  type        = string
  default     = ""
}

variable "route53_zone_id" {
  description = "Hosted zone id de Route 53 para validar el cert automáticamente. Vacío si el dominio vive fuera."
  type        = string
  default     = ""
}

resource "aws_acm_certificate" "main" {
  count             = var.domain_name == "" ? 0 : 1
  domain_name       = var.domain_name
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "acm_validation" {
  for_each = var.domain_name == "" || var.route53_zone_id == "" ? {} : {
    for dvo in aws_acm_certificate.main[0].domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }

  allow_overwrite = true
  name            = each.value.name
  records         = [each.value.record]
  ttl             = 60
  type            = each.value.type
  zone_id         = var.route53_zone_id
}

resource "aws_acm_certificate_validation" "main" {
  count                   = var.domain_name == "" || var.route53_zone_id == "" ? 0 : 1
  certificate_arn         = aws_acm_certificate.main[0].arn
  validation_record_fqdns = [for record in aws_route53_record.acm_validation : record.fqdn]
}

resource "aws_route53_record" "alb_alias" {
  count   = var.domain_name == "" || var.route53_zone_id == "" ? 0 : 1
  zone_id = var.route53_zone_id
  name    = var.domain_name
  type    = "A"

  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}
