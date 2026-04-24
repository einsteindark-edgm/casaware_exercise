# ── ECS Backend task def + service (Phase A.8) ───────────────────────

variable "backend_image_tag" {
  description = "Tag de la imagen backend en ECR. En primer apply, dejar 'bootstrap' (no se crea service hasta haber pusheado)."
  type        = string
  default     = "bootstrap"
}

variable "backend_desired_count" {
  description = "Cuántas tasks del backend correr. 0 al principio, subir a 1 cuando la imagen esté en ECR."
  type        = number
  default     = 0
}

locals {
  backend_container_name = "backend"
}

resource "aws_ecs_task_definition" "backend" {
  family                   = "${var.prefix}-backend"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.backend_task.arn

  container_definitions = jsonencode([{
    name      = local.backend_container_name
    image     = "${aws_ecr_repository.backend.repository_url}:${var.backend_image_tag}"
    essential = true

    portMappings = [{
      containerPort = 8000
      protocol      = "tcp"
    }]

    environment = [
      { name = "ENV", value = "dev" },
      { name = "AWS_REGION", value = var.aws_region },
      { name = "AUTH_MODE", value = "prod" },
      { name = "TEMPORAL_MODE", value = "real" },
      { name = "TEMPORAL_NAMESPACE", value = "default" },
      { name = "COGNITO_USER_POOL_ID", value = aws_cognito_user_pool.main.id },
      { name = "COGNITO_APP_CLIENT_ID", value = aws_cognito_user_pool_client.web.id },
      { name = "COGNITO_JWKS_URL", value = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.main.id}/.well-known/jwks.json" },
      { name = "S3_RECEIPTS_BUCKET", value = aws_s3_bucket.receipts.bucket },
      { name = "S3_TEXTRACT_OUTPUT_BUCKET", value = aws_s3_bucket.textract_output.bucket },
      { name = "MONGODB_DB", value = "nexus_dev" },
      { name = "REDIS_TLS", value = "false" },
      { name = "LOG_LEVEL", value = "INFO" },
      { name = "XRAY_ENABLED", value = "false" },
      { name = "CORS_ORIGINS", value = jsonencode([local.public_base_url]) },
    ]

    secrets = [
      { name = "MONGODB_URI", valueFrom = aws_secretsmanager_secret.mongodb_uri.arn },
      { name = "REDIS_URL", valueFrom = aws_secretsmanager_secret.redis_url.arn },
      { name = "TEMPORAL_HOST", valueFrom = aws_secretsmanager_secret.temporal_ngrok_url.arn },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.backend.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "ecs"
      }
    }
  }])
}

resource "aws_ecs_service" "backend" {
  name                               = "${var.prefix}-backend"
  cluster                            = aws_ecs_cluster.main.id
  task_definition                    = aws_ecs_task_definition.backend.arn
  launch_type                        = "FARGATE"
  desired_count                      = var.backend_desired_count
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = 180

  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.backend.id]
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.backend.arn
    container_name   = local.backend_container_name
    container_port   = 8000
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.backend_sse.arn
    container_name   = local.backend_container_name
    container_port   = 8000
  }

  depends_on = [
    aws_lb_listener.http,
    aws_lb_listener_rule.backend_api,
    aws_lb_listener_rule.backend_sse,
  ]

  lifecycle {
    # task_definition is managed out-of-band by .github/workflows/build-and-push.yml
    # (registers a new revision per push to main). See ecs_workers.tf for context.
    ignore_changes = [desired_count, task_definition]
  }
}
