# ── Receipts bucket: user-uploaded PDFs/images. Textract reads from here. ───

resource "aws_s3_bucket" "receipts" {
  bucket        = "${var.prefix}-receipts"
  force_destroy = true
}

resource "aws_s3_bucket_versioning" "receipts" {
  bucket = aws_s3_bucket.receipts.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "receipts" {
  bucket = aws_s3_bucket.receipts.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "receipts" {
  bucket = aws_s3_bucket.receipts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_cors_configuration" "receipts" {
  bucket = aws_s3_bucket.receipts.id

  cors_rule {
    allowed_headers = ["*"]
    allowed_methods = ["GET", "PUT", "POST"]
    allowed_origins = ["http://localhost:3000"]
    expose_headers  = ["ETag"]
    max_age_seconds = 3000
  }
}

# ── Textract output bucket: raw AnalyzeExpense JSON, for lineage. ───────────

resource "aws_s3_bucket" "textract_output" {
  bucket        = "${var.prefix}-textract-output"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "textract_output" {
  bucket = aws_s3_bucket.textract_output.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "textract_output" {
  bucket = aws_s3_bucket.textract_output.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Lineage artifacts aren't business-critical — expire after 30 days in dev to
# keep S3 costs near zero.
resource "aws_s3_bucket_lifecycle_configuration" "textract_output" {
  bucket = aws_s3_bucket.textract_output.id

  rule {
    id     = "expire-old-outputs"
    status = "Enabled"

    filter {}

    expiration {
      days = 30
    }
  }
}
