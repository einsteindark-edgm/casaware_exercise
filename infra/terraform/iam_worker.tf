resource "aws_iam_user" "worker" {
  name = "${var.prefix}-worker"
}

resource "aws_iam_access_key" "worker" {
  user = aws_iam_user.worker.name
}

data "aws_iam_policy_document" "worker" {
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
    sid    = "S3Buckets"
    effect = "Allow"
    actions = [
      "s3:ListBucket",
      "s3:GetBucketLocation",
    ]
    resources = [
      aws_s3_bucket.receipts.arn,
      aws_s3_bucket.textract_output.arn,
    ]
  }

  statement {
    sid    = "Textract"
    effect = "Allow"
    actions = [
      "textract:AnalyzeExpense",
      "textract:AnalyzeDocument",
      "textract:StartExpenseAnalysis",
      "textract:GetExpenseAnalysis",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_user_policy" "worker" {
  name   = "${var.prefix}-worker-policy"
  user   = aws_iam_user.worker.name
  policy = data.aws_iam_policy_document.worker.json
}
