# ── ECS Temporal workers (Phase A.9) ────────────────────────────────
#
# Un solo service con TASK_QUEUE=all para mantener el footprint pequeño.
# Cuando se necesite aislar queues (doc 03-orquestacion-temporal.md §13)
# se split en 4 services: orchestrator / ocr / databricks / rag.

variable "worker_image_tag" {
  description = "Tag de la imagen del worker en ECR."
  type        = string
  default     = "bootstrap"
}

variable "worker_desired_count" {
  description = "Cuántas tasks de worker correr. 0 antes del primer push."
  type        = number
  default     = 0
}

locals {
  worker_container_name = "worker"
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${var.prefix}-worker"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.worker_task.arn

  container_definitions = jsonencode([{
    name      = local.worker_container_name
    image     = "${aws_ecr_repository.worker.repository_url}:${var.worker_image_tag}"
    essential = true

    environment = [
      { name = "ENV", value = "dev" },
      { name = "AWS_REGION", value = var.aws_region },
      { name = "TEMPORAL_NAMESPACE", value = "default" },
      # `all` = una task atiende las 4 queues. Ajustar si se split en 4.
      { name = "TASK_QUEUE", value = "all" },
      { name = "MONGODB_DB", value = "nexus_dev" },
      { name = "REDIS_TLS", value = "false" },
      { name = "S3_RECEIPTS_BUCKET", value = aws_s3_bucket.receipts.bucket },
      { name = "S3_TEXTRACT_OUTPUT_BUCKET", value = aws_s3_bucket.textract_output.bucket },
      { name = "FAKE_PROVIDERS", value = "true" },
      { name = "FAKE_HITL_MODE", value = "force" },
      # Phase C: Vector Search real mientras Textract/Bedrock siguen fake.
      # Flip a "false" una vez que el index Databricks este poblado.
      { name = "FAKE_VECTOR_SEARCH", value = "true" },
      { name = "AUDIT_SOURCE", value = "mongo" },
      { name = "LOG_LEVEL", value = "INFO" },
      { name = "WORKER_MAX_CONCURRENT_ACTIVITIES", value = "20" },
      { name = "WORKER_MAX_CONCURRENT_WORKFLOW_TASKS", value = "50" },
      # boto3 usa el task role via IMDSv2; no hay AWS_ENDPOINT_URL (prod real).
      { name = "AWS_ENDPOINT_URL", value = "" },
      # Databricks Vector Search (tomados de SSM plaintext params).
      { name = "DATABRICKS_VS_ENDPOINT", value = aws_ssm_parameter.databricks_vs_endpoint.value },
      { name = "DATABRICKS_VS_INDEX", value = aws_ssm_parameter.databricks_vs_index.value },
      { name = "DATABRICKS_CATALOG", value = var.databricks_catalog_name },
    ]

    secrets = [
      { name = "MONGODB_URI", valueFrom = aws_secretsmanager_secret.mongodb_uri.arn },
      { name = "REDIS_URL", valueFrom = aws_secretsmanager_secret.redis_url.arn },
      { name = "TEMPORAL_HOST", valueFrom = aws_secretsmanager_secret.temporal_ngrok_url.arn },
      { name = "DATABRICKS_HOST", valueFrom = aws_secretsmanager_secret.databricks_host.arn },
      { name = "DATABRICKS_TOKEN", valueFrom = aws_secretsmanager_secret.databricks_token.arn },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.worker.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "ecs"
      }
    }
  }])
}

resource "aws_ecs_service" "worker" {
  name                               = "${var.prefix}-worker"
  cluster                            = aws_ecs_cluster.main.id
  task_definition                    = aws_ecs_task_definition.worker.arn
  launch_type                        = "FARGATE"
  desired_count                      = var.worker_desired_count
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 200

  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.worker.id]
  }

  lifecycle {
    ignore_changes = [desired_count]
  }
}
