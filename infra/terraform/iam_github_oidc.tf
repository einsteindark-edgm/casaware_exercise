resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]

  # GitHub's published thumbprints. AWS recommends keeping both known values
  # here to survive GitHub certificate rotations.
  thumbprint_list = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f2df264fcd",
  ]
}

data "aws_iam_policy_document" "gha_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:${var.github_repo}:ref:refs/heads/main",
        "repo:${var.github_repo}:pull_request",
      ]
    }
  }
}

resource "aws_iam_role" "gha_terraform" {
  name               = "${var.prefix}-gha-terraform"
  assume_role_policy = data.aws_iam_policy_document.gha_trust.json
}

# Scoped to what this repo's Terraform actually manages. Broaden later only
# if new resource types are added.
data "aws_iam_policy_document" "gha_terraform_policy" {
  statement {
    sid    = "S3Manage"
    effect = "Allow"
    actions = [
      "s3:*",
    ]
    resources = [
      aws_s3_bucket.receipts.arn,
      "${aws_s3_bucket.receipts.arn}/*",
    ]
  }

  statement {
    sid    = "S3ReadForPlan"
    effect = "Allow"
    actions = [
      "s3:ListAllMyBuckets",
      "s3:GetBucketLocation",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "IAMManage"
    effect = "Allow"
    actions = [
      "iam:GetUser",
      "iam:GetRole",
      "iam:GetPolicy",
      "iam:GetRolePolicy",
      "iam:GetUserPolicy",
      "iam:GetOpenIDConnectProvider",
      "iam:ListAttachedRolePolicies",
      "iam:ListRolePolicies",
      "iam:ListUserPolicies",
      "iam:ListAccessKeys",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "gha_terraform" {
  name   = "${var.prefix}-gha-terraform-policy"
  role   = aws_iam_role.gha_terraform.id
  policy = data.aws_iam_policy_document.gha_terraform_policy.json
}
