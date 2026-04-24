# ── Phase B+ · Databricks → MSK IAM auth ─────────────────────────────
#
# DBR 16.1+ soporta Unity Catalog Service Credentials para consumir
# MSK con IAM SASL sin tener que poner sasl.jaas.config inline en el
# notebook. El pipeline DLT usa:
#
#   spark.readStream.format("kafka")
#     .option("kafka.bootstrap.servers", <bootstrap>)
#     .option("kafka.security.protocol", "SASL_SSL")
#     .option("kafka.sasl.mechanism", "AWS_MSK_IAM")
#     .option("databricks.serviceCredential", "nexus-msk-cred")
#
# Detrás de escena Databricks asume aws_iam_role.databricks_msk_access
# (via su StorageCredentials trust) y usa esas creds para firmar SASL.
#
# Todo gateado por var.msk_enabled + var.databricks_enabled — solo
# funciona cuando ambos stacks están arriba.

locals {
  databricks_msk_enabled = var.msk_enabled && var.databricks_enabled
}

# ── IAM role asumido por Databricks para llegar a MSK ────────────────
# Mismo patrón que uc_access: trust hacia el UC master role + external
# ID, más self-assume (ver comentario en databricks.tf).

data "aws_iam_policy_document" "databricks_msk_trust" {
  count = local.databricks_msk_enabled ? 1 : 0

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

  # Self-assume: mismo workaround que uc_access. Terraform ignora
  # cambios al trust post-creación; el update real va por AWS CLI.
  statement {
    sid     = "SelfAssume"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${var.prefix}-msk-databricks"]
    }
  }
}

resource "aws_iam_role" "databricks_msk_access" {
  count              = local.databricks_msk_enabled ? 1 : 0
  name               = "${var.prefix}-msk-databricks"
  assume_role_policy = data.aws_iam_policy_document.databricks_msk_trust[0].json

  lifecycle {
    ignore_changes = [assume_role_policy]
  }
}

# Permisos MSK: connect al cluster, describe/read sobre todos los
# topics, consumer groups.
data "aws_iam_policy_document" "databricks_msk_access" {
  count = local.databricks_msk_enabled ? 1 : 0

  statement {
    sid       = "MSKConnect"
    effect    = "Allow"
    actions   = ["kafka-cluster:Connect", "kafka-cluster:DescribeCluster"]
    resources = [aws_msk_serverless_cluster.nexus[0].arn]
  }

  statement {
    sid    = "MSKTopicRead"
    effect = "Allow"
    actions = [
      "kafka-cluster:DescribeTopic",
      "kafka-cluster:ReadData",
      "kafka-cluster:DescribeTopicDynamicConfiguration",
    ]
    resources = [replace(aws_msk_serverless_cluster.nexus[0].arn, ":cluster/", ":topic/")]
  }

  statement {
    sid    = "MSKGroupReadWrite"
    effect = "Allow"
    actions = [
      "kafka-cluster:AlterGroup",
      "kafka-cluster:DescribeGroup",
    ]
    resources = [replace(aws_msk_serverless_cluster.nexus[0].arn, ":cluster/", ":group/")]
  }
}

resource "aws_iam_role_policy" "databricks_msk_access" {
  count  = local.databricks_msk_enabled ? 1 : 0
  name   = "${var.prefix}-msk-databricks"
  role   = aws_iam_role.databricks_msk_access[0].id
  policy = data.aws_iam_policy_document.databricks_msk_access[0].json
}

# ── Unity Catalog Service Credential ─────────────────────────────────
# Registra el role en UC para que el pipeline DLT lo pueda referenciar
# por nombre en spark.conf.

resource "databricks_credential" "nexus_msk" {
  count   = local.databricks_msk_enabled ? 1 : 0
  name    = "${var.prefix}-msk-cred"
  purpose = "SERVICE"
  comment = "Service credential que el pipeline bronze_cdc usa para consumir MSK via IAM SASL."

  aws_iam_role {
    role_arn = aws_iam_role.databricks_msk_access[0].arn
  }

  depends_on = [aws_iam_role_policy.databricks_msk_access]
}

# ── Output ───────────────────────────────────────────────────────────

output "databricks_msk_service_credential" {
  description = "Nombre del UC Service Credential para referencias desde DLT."
  value       = local.databricks_msk_enabled ? databricks_credential.nexus_msk[0].name : ""
}
