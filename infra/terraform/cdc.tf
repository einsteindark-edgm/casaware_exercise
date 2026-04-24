# ── Phase B: CDC Mongo → Bronze (Debezium-lite) ─────────────────────
#
# Infra para el servicio nexus-cdc: ECR, S3 bucket de eventos, DynamoDB
# de offsets, IAM role, CloudWatch log group, ECS task + service.
# Reutiliza el worker security group (egress-only).

variable "cdc_image_tag" {
  description = "Tag de la imagen del CDC listener en ECR."
  type        = string
  default     = "bootstrap"
}

variable "cdc_desired_count" {
  description = "Cuántas tasks del listener correr. 0 antes del primer push."
  type        = number
  default     = 0
}

locals {
  cdc_container_name = "cdc"
}

# ── ECR ──────────────────────────────────────────────────────────────

resource "aws_ecr_repository" "cdc" {
  name                 = "${var.prefix}-cdc"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "cdc" {
  repository = aws_ecr_repository.cdc.name
  policy     = local.ecr_lifecycle_policy
}

# ── S3 bucket para eventos CDC ───────────────────────────────────────

resource "aws_s3_bucket" "cdc" {
  bucket        = "${var.prefix}-cdc"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "cdc" {
  bucket = aws_s3_bucket.cdc.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "cdc" {
  bucket = aws_s3_bucket.cdc.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "cdc" {
  bucket = aws_s3_bucket.cdc.id

  # Eventos CDC son append-only. En dev expiramos a los 90d para evitar
  # ballooning de storage. El "ground truth" vive en Mongo + bronze Delta.
  rule {
    id     = "expire-cdc-events"
    status = "Enabled"

    filter { prefix = "" }

    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }

    expiration {
      days = 90
    }
  }

  # DLQ se retiene más tiempo — son incidentes a investigar.
  rule {
    id     = "dlq-longer-retention"
    status = "Enabled"

    filter { prefix = "dlq/" }

    expiration {
      days = 365
    }
  }

  # Autoloader state (checkpoints del pipeline) NO se toca por lifecycle.
  rule {
    id     = "keep-autoloader-state"
    status = "Enabled"

    filter { prefix = "_autoloader_state/" }

    expiration {
      days = 3650
    }
  }
}

# ── DynamoDB para resume tokens ──────────────────────────────────────

resource "aws_dynamodb_table" "cdc_offsets" {
  name         = "${var.prefix}-cdc-offsets"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "collection"

  attribute {
    name = "collection"
    type = "S"
  }

  point_in_time_recovery {
    enabled = false
  }
}

# ── CloudWatch Log Group ─────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "cdc" {
  name              = "/ecs/${var.prefix}/cdc"
  retention_in_days = 14
}

# ── IAM Task Role ────────────────────────────────────────────────────

resource "aws_iam_role" "cdc_task" {
  name               = "${var.prefix}-ecs-task-cdc"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_execution_assume.json
}

data "aws_iam_policy_document" "cdc_task_policy" {
  statement {
    sid    = "S3Put"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:GetObject",
      "s3:DeleteObject",
    ]
    resources = ["${aws_s3_bucket.cdc.arn}/*"]
  }

  statement {
    sid       = "S3Bucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.cdc.arn]
  }

  statement {
    sid    = "Dynamo"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
    ]
    resources = [aws_dynamodb_table.cdc_offsets.arn]
  }
}

resource "aws_iam_role_policy" "cdc_task" {
  name   = "${var.prefix}-ecs-task-cdc"
  role   = aws_iam_role.cdc_task.id
  policy = data.aws_iam_policy_document.cdc_task_policy.json
}

# El execution role ya existe (ecs_task_execution); hay que extender su
# policy para que pueda leer el secret de Mongo — ya está ahí por el
# worker, reutilizamos.

# ── ECS task definition + service ────────────────────────────────────

resource "aws_ecs_task_definition" "cdc" {
  family                   = "${var.prefix}-cdc"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.cdc_task.arn

  container_definitions = jsonencode([{
    name      = local.cdc_container_name
    image     = "${aws_ecr_repository.cdc.repository_url}:${var.cdc_image_tag}"
    essential = true

    environment = [
      { name = "AWS_REGION", value = var.aws_region },
      { name = "MONGODB_DB", value = "nexus_dev" },
      { name = "S3_BUCKET", value = aws_s3_bucket.cdc.bucket },
      { name = "DDB_OFFSETS_TABLE", value = aws_dynamodb_table.cdc_offsets.name },
      { name = "BATCH_MAX_EVENTS", value = "200" },
      { name = "BATCH_MAX_SECONDS", value = "30" },
      { name = "IDLE_FLUSH_SECONDS", value = "300" },
      { name = "LOG_LEVEL", value = "INFO" },
      { name = "BOOTSTRAP_ON_EMPTY_OFFSET", value = "true" },
    ]

    secrets = [
      { name = "MONGODB_URI", valueFrom = aws_secretsmanager_secret.mongodb_uri.arn },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.cdc.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "ecs"
      }
    }
  }])
}

resource "aws_ecs_service" "cdc" {
  name                               = "${var.prefix}-cdc"
  cluster                            = aws_ecs_cluster.main.id
  task_definition                    = aws_ecs_task_definition.cdc.arn
  launch_type                        = "FARGATE"
  desired_count                      = var.cdc_desired_count
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 200

  network_configuration {
    # Same private subnets + egress-only SG as worker. Mongo Atlas access
    # goes out via NAT; S3 via gateway endpoint (free).
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.worker.id]
  }

  lifecycle {
    ignore_changes = [desired_count]
  }
}

# ── SSM params exponen bucket/table al equipo Databricks ─────────────

resource "aws_ssm_parameter" "cdc_bucket" {
  name  = "/${var.prefix}/cdc_bucket"
  type  = "String"
  value = aws_s3_bucket.cdc.bucket
}

resource "aws_ssm_parameter" "cdc_offsets_table" {
  name  = "/${var.prefix}/cdc_offsets_table"
  type  = "String"
  value = aws_dynamodb_table.cdc_offsets.name
}

# ── Outputs ──────────────────────────────────────────────────────────

output "cdc_ecr_repository_url" {
  value = aws_ecr_repository.cdc.repository_url
}

output "cdc_s3_bucket" {
  value = aws_s3_bucket.cdc.bucket
}

output "cdc_ddb_offsets_table" {
  value = aws_dynamodb_table.cdc_offsets.name
}
