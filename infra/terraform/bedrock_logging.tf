# ── Phase E.6 — Bedrock Invocation Logging ───────────────────────────
#
# Activa el logging account-level de Amazon Bedrock para que cada
# `Converse` / `InvokeModel` quede registrado con el prompt completo,
# tool_use blocks emitidos y respuesta del modelo. Sin esto NO hay
# visibilidad de las decisiones del agente — las activities Temporal
# solo ven el resultado final.
#
# Importante:
#  - El config de Bedrock logging es ÚNICO por cuenta+región. Si ya
#    existe otra config, este resource va a sobreescribirla. Verificá
#    con `aws bedrock get-model-invocation-logging-configuration`.
#  - `text_data_delivery_enabled = true` captura prompts y respuestas;
#    embeddings e imágenes quedan apagados por costo y privacidad.
#  - El log group queda en CloudWatch con retención corta (14d) — los
#    prompts pueden ser pesados. S3 sería más barato a largo plazo;
#    si te interesa, agregamos un bucket dedicado en una iteración.

resource "aws_cloudwatch_log_group" "bedrock_invocations" {
  name              = "/aws/bedrock/${var.prefix}/modelinvocations"
  retention_in_days = 14
}

# Bedrock service principal (no es un role asumido por una task ECS,
# es asumido por el servicio Bedrock de tu cuenta).
data "aws_iam_policy_document" "bedrock_logging_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "bedrock_logging" {
  name               = "${var.prefix}-bedrock-logging"
  assume_role_policy = data.aws_iam_policy_document.bedrock_logging_assume.json
}

data "aws_iam_policy_document" "bedrock_logging" {
  statement {
    sid    = "WriteToLogGroup"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [
      "${aws_cloudwatch_log_group.bedrock_invocations.arn}:*",
    ]
  }
}

resource "aws_iam_role_policy" "bedrock_logging" {
  name   = "${var.prefix}-bedrock-logging"
  role   = aws_iam_role.bedrock_logging.id
  policy = data.aws_iam_policy_document.bedrock_logging.json
}

resource "aws_bedrock_model_invocation_logging_configuration" "main" {
  depends_on = [aws_iam_role_policy.bedrock_logging]

  logging_config {
    embedding_data_delivery_enabled = false
    image_data_delivery_enabled     = false
    text_data_delivery_enabled      = true
    video_data_delivery_enabled     = false

    cloudwatch_config {
      log_group_name = aws_cloudwatch_log_group.bedrock_invocations.name
      role_arn       = aws_iam_role.bedrock_logging.arn
    }
  }
}

output "bedrock_invocations_log_group" {
  description = "CloudWatch log group con prompts + respuestas + tool_use de cada invocación Bedrock."
  value       = aws_cloudwatch_log_group.bedrock_invocations.name
}
