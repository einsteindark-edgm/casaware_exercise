# ── ECS Frontend task def + service (Phase A.8) ──────────────────────

variable "frontend_image_tag" {
  description = "Tag de la imagen frontend en ECR."
  type        = string
  default     = "bootstrap"
}

variable "frontend_desired_count" {
  description = "Cuántas tasks del frontend correr. 0 al principio."
  type        = number
  default     = 0
}

locals {
  frontend_container_name = "frontend"

  # Base URL público: el dominio si existe, si no el DNS del ALB.
  public_base_url = var.domain_name != "" ? "https://${var.domain_name}" : "http://${aws_lb.main.dns_name}"
}

resource "aws_ecs_task_definition" "frontend" {
  family                   = "${var.prefix}-frontend"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.frontend_task.arn

  container_definitions = jsonencode([{
    name      = local.frontend_container_name
    image     = "${aws_ecr_repository.frontend.repository_url}:${var.frontend_image_tag}"
    essential = true

    portMappings = [{
      containerPort = 3000
      protocol      = "tcp"
    }]

    environment = [
      { name = "NODE_ENV", value = "production" },
      { name = "NEXT_PUBLIC_AUTH_MODE", value = "prod" },
      { name = "NEXT_PUBLIC_APP_URL", value = local.public_base_url },
      { name = "NEXT_PUBLIC_API_URL", value = local.public_base_url },
      { name = "NEXT_PUBLIC_COGNITO_USER_POOL_ID", value = aws_cognito_user_pool.main.id },
      { name = "NEXT_PUBLIC_COGNITO_APP_CLIENT_ID", value = aws_cognito_user_pool_client.web.id },
      { name = "NEXT_PUBLIC_COGNITO_DOMAIN", value = "${aws_cognito_user_pool_domain.main.domain}.auth.${var.aws_region}.amazoncognito.com" },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.frontend.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "ecs"
      }
    }
  }])
}

resource "aws_ecs_service" "frontend" {
  name                               = "${var.prefix}-frontend"
  cluster                            = aws_ecs_cluster.main.id
  task_definition                    = aws_ecs_task_definition.frontend.arn
  launch_type                        = "FARGATE"
  desired_count                      = var.frontend_desired_count
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = 60

  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.frontend.id]
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.frontend.arn
    container_name   = local.frontend_container_name
    container_port   = 3000
  }

  depends_on = [aws_lb_listener.http]

  lifecycle {
    ignore_changes = [desired_count]
  }
}
