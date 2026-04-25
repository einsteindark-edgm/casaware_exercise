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
      # Stretch the ECS task-role credential fetch timeouts (Bedrock/S3/
      # Textract activities all share the same provider chain).
      { name = "AWS_METADATA_SERVICE_TIMEOUT", value = "10" },
      { name = "AWS_METADATA_SERVICE_NUM_ATTEMPTS", value = "5" },
      { name = "TEMPORAL_NAMESPACE", value = "default" },
      # `all` = una task atiende las 4 queues. Ajustar si se split en 4.
      { name = "TASK_QUEUE", value = "all" },
      { name = "MONGODB_DB", value = "nexus_dev" },
      { name = "REDIS_TLS", value = "false" },
      { name = "S3_RECEIPTS_BUCKET", value = aws_s3_bucket.receipts.bucket },
      { name = "S3_TEXTRACT_OUTPUT_BUCKET", value = aws_s3_bucket.textract_output.bucket },
      # All real providers active. Flip individual FAKE_* to "true" only as
      # an emergency fallback if one provider is unavailable.
      { name = "FAKE_PROVIDERS", value = "false" },
      { name = "FAKE_HITL_MODE", value = "auto" },
      { name = "FAKE_TEXTRACT", value = "false" },
      { name = "FAKE_BEDROCK", value = "false" },
      { name = "FAKE_VECTOR_SEARCH", value = "false" },
      { name = "FAKE_SQL_SEARCH", value = "false" },
      { name = "AUDIT_SOURCE", value = "mongo" },
      { name = "LOG_LEVEL", value = "INFO" },
      { name = "WORKER_MAX_CONCURRENT_ACTIVITIES", value = "20" },
      { name = "WORKER_MAX_CONCURRENT_WORKFLOW_TASKS", value = "50" },
      # boto3 usa el task role via IMDSv2; no hay AWS_ENDPOINT_URL (prod real).
      { name = "AWS_ENDPOINT_URL", value = "" },
      # Databricks Vector Search + SQL (tomados de SSM plaintext params).
      { name = "DATABRICKS_VS_ENDPOINT", value = aws_ssm_parameter.databricks_vs_endpoint.value },
      { name = "DATABRICKS_VS_INDEX", value = aws_ssm_parameter.databricks_vs_index.value },
      { name = "DATABRICKS_CATALOG", value = var.databricks_catalog_name },
      # Bedrock model — Amazon Nova Pro via US cross-region inference profile.
      # Nova requires no use-case form; idéntico shape de Converse a Claude.
      { name = "BEDROCK_MODEL_ID", value = "us.amazon.nova-pro-v1:0" },
      # Vector search backend: "local" (in-process cosine over precomputed
      # embeddings) or "managed" (Mosaic AI VS managed index). Default local
      # because managed Delta Sync auto-pipelines depend on workspace tier.
      { name = "VECTOR_BACKEND", value = "local" },
      # Phase E.3 — OpenTelemetry → ADOT sidecar → X-Ray
      { name = "OTEL_ENABLED", value = "true" },
      { name = "OTEL_EXPORTER_OTLP_ENDPOINT", value = "http://localhost:4317" },
      { name = "OTEL_EXPORTER_OTLP_PROTOCOL", value = "grpc" },
      { name = "OTEL_SERVICE_NAME", value = "nexus-worker" },
      { name = "OTEL_RESOURCE_ATTRIBUTES", value = "service.namespace=nexus,deployment.environment=dev" },
      { name = "OTEL_PROPAGATORS", value = "tracecontext,xray" },
      { name = "OTEL_TRACES_SAMPLER", value = "parentbased_traceidratio" },
      { name = "OTEL_TRACES_SAMPLER_ARG", value = "0.05" },
    ]

    secrets = [
      { name = "MONGODB_URI", valueFrom = aws_secretsmanager_secret.mongodb_uri.arn },
      { name = "REDIS_URL", valueFrom = aws_secretsmanager_secret.redis_url.arn },
      { name = "TEMPORAL_HOST", valueFrom = aws_secretsmanager_secret.temporal_ngrok_url.arn },
      { name = "DATABRICKS_HOST", valueFrom = aws_secretsmanager_secret.databricks_host.arn },
      { name = "DATABRICKS_TOKEN", valueFrom = aws_secretsmanager_secret.databricks_token.arn },
      { name = "DATABRICKS_WAREHOUSE_ID", valueFrom = aws_secretsmanager_secret.databricks_warehouse_id.arn },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.worker.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "ecs"
      }
    }

    dependsOn = [
      { containerName = "adot-collector", condition = "START" }
    ]
    },
    # Phase E.1 — ADOT sidecar (OTel → X-Ray + CloudWatch metrics)
    {
      name      = "adot-collector"
      image     = "public.ecr.aws/aws-observability/aws-otel-collector:v0.40.0"
      essential = false
      cpu       = 64
      memory    = 128

      command = ["--config=/etc/ecs/ecs-default-config.yaml"]

      environment = [
        { name = "AWS_REGION", value = var.aws_region },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.adot.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "worker"
        }
      }

      portMappings = [
        { containerPort = 4317, protocol = "tcp" },
        { containerPort = 4318, protocol = "tcp" },
      ]
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
    # task_definition is managed out-of-band by .github/workflows/build-and-push.yml
    # (each push to main registers a new revision pointing at the new image tag).
    # Without ignoring it, `terraform apply` reverts the service to the TF-tracked
    # revision and resurrects the previous image — which is how the _parse_amount
    # fix in comparison.py got rolled back to v20260423-2236.
    ignore_changes = [desired_count, task_definition]
  }
}
