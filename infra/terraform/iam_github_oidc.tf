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
      aws_s3_bucket.textract_output.arn,
      "${aws_s3_bucket.textract_output.arn}/*",
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
      "iam:PassRole",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "EcrLogin"
    effect = "Allow"
    actions = [
      "ecr:GetAuthorizationToken",
    ]
    resources = ["*"]
  }

  # Frontend build-args are sourced from SSM at GHA build time. Without
  # this permission the `aws ssm get-parameter` call silently returned
  # empty strings, which baked `NEXT_PUBLIC_COGNITO_USER_POOL_ID=""` into
  # the static bundle and produced "Auth UserPool not configured" in the
  # browser. See `.github/workflows/build-and-push.yml` step `fetch
  # frontend build-args from SSM`.
  statement {
    sid    = "SsmReadBuildArgs"
    effect = "Allow"
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
    ]
    resources = [
      "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.prefix}/*",
    ]
  }

  statement {
    sid    = "EcrPushPull"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
      "ecr:DescribeImages",
      "ecr:DescribeRepositories",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
    ]
    resources = [
      aws_ecr_repository.backend.arn,
      aws_ecr_repository.frontend.arn,
      aws_ecr_repository.worker.arn,
    ]
  }

  statement {
    sid    = "EcsDeploy"
    effect = "Allow"
    actions = [
      "ecs:DescribeServices",
      "ecs:DescribeTaskDefinition",
      "ecs:RegisterTaskDefinition",
      "ecs:UpdateService",
      "ecs:ListTasks",
      "ecs:DescribeTasks",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "gha_terraform" {
  name   = "${var.prefix}-gha-terraform-policy"
  role   = aws_iam_role.gha_terraform.id
  policy = data.aws_iam_policy_document.gha_terraform_policy.json
}
