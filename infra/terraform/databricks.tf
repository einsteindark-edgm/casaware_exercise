# ── Phase C · Databricks Unity Catalog ───────────────────────────────
#
# Crea:
#   - S3 bucket para el catalog managed storage (uc-root).
#   - IAM role que UC asume para acceder al bucket (cross-account).
#   - Storage credential en UC apuntando a ese role.
#   - External location sobre el bucket.
#   - Catalog nexus_dev con 5 schemas: bronze, silver, gold, vector, security.
#
# Todos los recursos estan gateados por var.databricks_enabled. Despues
# de completar el runbook C.0, poner en terraform.tfvars:
#
#   databricks_enabled      = true
#   databricks_host         = "https://dbc-...cloud.databricks.com"
#   databricks_token        = "dapi..."
#   databricks_metastore_id = "abcd1234-..."
#
# Y correr: terraform apply -target=databricks_catalog.nexus_dev

# ── S3 bucket para Unity Catalog managed storage ─────────────────────

resource "aws_s3_bucket" "uc_root" {
  count         = var.databricks_enabled ? 1 : 0
  bucket        = "${var.prefix}-uc"
  force_destroy = true
}

resource "aws_s3_bucket_versioning" "uc_root" {
  count  = var.databricks_enabled ? 1 : 0
  bucket = aws_s3_bucket.uc_root[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "uc_root" {
  count                   = var.databricks_enabled ? 1 : 0
  bucket                  = aws_s3_bucket.uc_root[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "uc_root" {
  count  = var.databricks_enabled ? 1 : 0
  bucket = aws_s3_bucket.uc_root[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# ── IAM role asumido por Databricks UC ───────────────────────────────
#
# Databricks usa un trust policy self-assume (el role se asume a si
# mismo) + externaId = metastore_id. Ver:
#   https://docs.databricks.com/aws/en/connect/unity-catalog/cloud-storage/storage-credentials

data "aws_iam_policy_document" "uc_trust" {
  count = var.databricks_enabled ? 1 : 0

  statement {
    sid     = "DatabricksAssume"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::414351767826:role/unity-catalog-prod-UCMasterRole-14S5ZJVKOTYTL"]
    }
    condition {
      test     = "StringEquals"
      variable = "sts:ExternalId"
      values   = [var.databricks_uc_external_id]
    }
  }

  # Self-assume: UC refresca credentials assumiendo el role sobre si
  # mismo. AWS rechaza esto en CREATE (el role aun no existe como
  # principal), pero lo acepta en UPDATE. Estrategia: ignore_changes
  # en el role para la creacion inicial, luego un update via AWS CLI
  # que deja self-assume in-place. Este bloque solo sirve para que
  # terraform NO marque drift.
  statement {
    sid     = "SelfAssume"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${var.prefix}-uc-access"]
    }
  }
}

resource "aws_iam_role" "uc_access" {
  count              = var.databricks_enabled ? 1 : 0
  name               = "${var.prefix}-uc-access"
  assume_role_policy = data.aws_iam_policy_document.uc_trust[0].json

  # Self-assume se aniade post-creacion via AWS CLI (no se puede en
  # CREATE porque el role no existe todavia como principal valido).
  # Sin este ignore_changes, terraform intentaria re-crear el role.
  lifecycle {
    ignore_changes = [assume_role_policy]
  }
}

data "aws_iam_policy_document" "uc_access" {
  count = var.databricks_enabled ? 1 : 0

  statement {
    sid    = "S3UCRoot"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
      "s3:GetBucketLocation",
      "s3:GetLifecycleConfiguration",
      "s3:PutLifecycleConfiguration",
    ]
    resources = [
      aws_s3_bucket.uc_root[0].arn,
      "${aws_s3_bucket.uc_root[0].arn}/*",
    ]
  }

  # UC tambien necesita leer los buckets de receipts / textract si
  # alguna tabla external apunta ahi (Auto Loader sobre textract_output).
  statement {
    sid    = "S3ExternalReadOnly"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:ListBucket",
      "s3:GetBucketLocation",
    ]
    resources = [
      aws_s3_bucket.receipts.arn,
      "${aws_s3_bucket.receipts.arn}/*",
      aws_s3_bucket.textract_output.arn,
      "${aws_s3_bucket.textract_output.arn}/*",
    ]
  }
}

resource "aws_iam_role_policy" "uc_access" {
  count  = var.databricks_enabled ? 1 : 0
  name   = "${var.prefix}-uc-access"
  role   = aws_iam_role.uc_access[0].id
  policy = data.aws_iam_policy_document.uc_access[0].json
}

# ── Databricks storage credential ────────────────────────────────────

resource "databricks_storage_credential" "nexus_uc" {
  count   = var.databricks_enabled ? 1 : 0
  name    = "${var.prefix}-uc-cred"
  comment = "Cross-account role used by UC to read/write s3://${aws_s3_bucket.uc_root[0].bucket}"

  aws_iam_role {
    role_arn = aws_iam_role.uc_access[0].arn
  }

  depends_on = [aws_iam_role_policy.uc_access]
}

resource "databricks_external_location" "uc_root" {
  count           = var.databricks_enabled ? 1 : 0
  name            = "${var.prefix}-uc-root"
  url             = "s3://${aws_s3_bucket.uc_root[0].bucket}/"
  credential_name = databricks_storage_credential.nexus_uc[0].name
  comment         = "Managed location for catalog ${var.databricks_catalog_name}."
}

# ── Catalog + schemas ────────────────────────────────────────────────

resource "databricks_catalog" "nexus_dev" {
  count          = var.databricks_enabled ? 1 : 0
  name           = var.databricks_catalog_name
  comment        = "Catalog raiz para el stack Nexus (${var.databricks_catalog_name})."
  storage_root   = databricks_external_location.uc_root[0].url
  isolation_mode = "ISOLATED"
  properties = {
    purpose = "nexus-medallion"
  }
}

locals {
  databricks_schemas = ["bronze", "silver", "gold", "vector", "security"]
}

resource "databricks_schema" "nexus" {
  for_each     = var.databricks_enabled ? toset(local.databricks_schemas) : toset([])
  catalog_name = databricks_catalog.nexus_dev[0].name
  name         = each.value
  comment      = "Medallion layer '${each.value}' - ver 05-medallion-databricks.md"
}
