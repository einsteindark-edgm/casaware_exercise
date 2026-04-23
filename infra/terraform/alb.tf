# ── Application Load Balancer (Phase A.7) ────────────────────────────
#
# Reglas:
#   priority 10  /api/*, /healthz         → backend TG
#   priority 20  /api/v1/events/*         → backend TG (sticky, SSE)
#   default                               → frontend TG
#
# Sticky sessions necesarias en /api/v1/events/* porque SSE mantiene la
# conexión abierta — si el LB rota, el cliente pierde la cola de eventos.

resource "aws_lb" "main" {
  name               = "${var.prefix}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  idle_timeout = 120 # SSE friendly

  tags = { Name = "${var.prefix}-alb" }
}

resource "aws_lb_target_group" "backend" {
  name        = "${var.prefix}-backend-tg"
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.main.id

  health_check {
    path                = "/healthz"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 5
  }

  deregistration_delay = 30

  tags = { Name = "${var.prefix}-backend-tg" }
}

resource "aws_lb_target_group" "backend_sse" {
  name        = "${var.prefix}-sse-tg"
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.main.id

  health_check {
    path                = "/healthz"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 5
  }

  stickiness {
    enabled         = true
    type            = "lb_cookie"
    cookie_duration = 3600
  }

  deregistration_delay = 30

  tags = { Name = "${var.prefix}-sse-tg" }
}

resource "aws_lb_target_group" "frontend" {
  name        = "${var.prefix}-frontend-tg"
  port        = 3000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.main.id

  health_check {
    path                = "/"
    matcher             = "200-399"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  deregistration_delay = 30

  tags = { Name = "${var.prefix}-frontend-tg" }
}

# HTTP listener: si hay cert ACM redirige a HTTPS, si no, sirve directo.
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = var.domain_name == "" ? "forward" : "redirect"

    dynamic "redirect" {
      for_each = var.domain_name == "" ? [] : [1]
      content {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }

    target_group_arn = var.domain_name == "" ? aws_lb_target_group.frontend.arn : null
  }
}

resource "aws_lb_listener" "https" {
  count             = var.domain_name == "" || var.route53_zone_id == "" ? 0 : 1
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.main[0].certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.frontend.arn
  }
}

# Reglas de path. Se enganchan al HTTPS listener si existe, si no al HTTP.
locals {
  active_listener_arn = length(aws_lb_listener.https) > 0 ? aws_lb_listener.https[0].arn : aws_lb_listener.http.arn
}

resource "aws_lb_listener_rule" "backend_api" {
  listener_arn = local.active_listener_arn
  priority     = 10

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.backend.arn
  }

  condition {
    path_pattern {
      values = ["/api/*", "/healthz"]
    }
  }
}

resource "aws_lb_listener_rule" "backend_sse" {
  listener_arn = local.active_listener_arn
  priority     = 5

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.backend_sse.arn
  }

  condition {
    path_pattern {
      values = ["/api/v1/events/*", "/api/v1/chat/stream/*"]
    }
  }
}
