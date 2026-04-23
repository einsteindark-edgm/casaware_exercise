# ── ECS Cluster + IAM roles comunes (Phase A.8) ──────────────────────

resource "aws_ecs_cluster" "main" {
  name = var.prefix

  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
    base              = 1
  }
}

# ── Task Execution Role ──────────────────────────────────────────────
# Usado por el agente ECS para pull de ECR, enviar logs y resolver secrets.

data "aws_iam_policy_document" "ecs_task_execution_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ecs_task_execution" {
  name               = "${var.prefix}-ecs-task-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_execution_assume.json
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution_managed" {
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "ecs_task_execution_extras" {
  statement {
    sid    = "ReadSecretsForInjection"
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
      "ssm:GetParameters",
      "ssm:GetParameter",
    ]
    resources = [
      aws_secretsmanager_secret.mongodb_uri.arn,
      aws_secretsmanager_secret.temporal_ngrok_url.arn,
      aws_secretsmanager_secret.redis_url.arn,
      "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.prefix}/*",
    ]
  }
}

resource "aws_iam_role_policy" "ecs_task_execution_extras" {
  name   = "${var.prefix}-ecs-task-execution-extras"
  role   = aws_iam_role.ecs_task_execution.id
  policy = data.aws_iam_policy_document.ecs_task_execution_extras.json
}

# ── Task Role: BACKEND ───────────────────────────────────────────────
# Lo usa el código backend en runtime (boto3 → S3, Textract).

resource "aws_iam_role" "backend_task" {
  name               = "${var.prefix}-ecs-task-backend"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_execution_assume.json
}

data "aws_iam_policy_document" "backend_task_policy" {
  statement {
    sid    = "S3Objects"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = [
      "${aws_s3_bucket.receipts.arn}/*",
      "${aws_s3_bucket.textract_output.arn}/*",
    ]
  }

  statement {
    sid       = "S3Buckets"
    effect    = "Allow"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.receipts.arn, aws_s3_bucket.textract_output.arn]
  }

  statement {
    sid    = "Textract"
    effect = "Allow"
    actions = [
      "textract:AnalyzeExpense",
      "textract:AnalyzeDocument",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "CognitoRead"
    effect    = "Allow"
    actions   = ["cognito-idp:AdminGetUser"]
    resources = [aws_cognito_user_pool.main.arn]
  }
}

resource "aws_iam_role_policy" "backend_task" {
  name   = "${var.prefix}-ecs-task-backend"
  role   = aws_iam_role.backend_task.id
  policy = data.aws_iam_policy_document.backend_task_policy.json
}

# ── Task Role: FRONTEND ──────────────────────────────────────────────
# Mínimo. Solo por si el SSR necesita leer un secret para Cognito.

resource "aws_iam_role" "frontend_task" {
  name               = "${var.prefix}-ecs-task-frontend"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_execution_assume.json
}

# ── Task Role: WORKER ────────────────────────────────────────────────
# Corre el código Temporal: lee/escribe S3, invoca Textract/Bedrock.

resource "aws_iam_role" "worker_task" {
  name               = "${var.prefix}-ecs-task-worker"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_execution_assume.json
}

data "aws_iam_policy_document" "worker_task_policy" {
  statement {
    sid    = "S3Objects"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = [
      "${aws_s3_bucket.receipts.arn}/*",
      "${aws_s3_bucket.textract_output.arn}/*",
    ]
  }

  statement {
    sid       = "S3Buckets"
    effect    = "Allow"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.receipts.arn, aws_s3_bucket.textract_output.arn]
  }

  statement {
    sid    = "Textract"
    effect = "Allow"
    actions = [
      "textract:AnalyzeExpense",
      "textract:AnalyzeDocument",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "Bedrock"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "worker_task" {
  name   = "${var.prefix}-ecs-task-worker"
  role   = aws_iam_role.worker_task.id
  policy = data.aws_iam_policy_document.worker_task_policy.json
}
