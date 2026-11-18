# GitHub Actions deploys with short-lived credentials from OIDC: no AWS keys are stored in GitHub.
# The deploy job runs in a GitHub environment (var.github_environment, "production"), so its
# token's subject is <subject prefix>:environment:<name> (not the branch). That environment only
# allows the main branch (deployment branch policy, set in GitHub), so only main can deploy.

# An account can hold only one provider per URL: the first environment creates it, later
# environments in the same account reuse it (create_github_oidc_provider = false).
resource "aws_iam_openid_connect_provider" "github" {
  count          = var.create_github_oidc_provider ? 1 : 0
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

moved {
  from = aws_iam_openid_connect_provider.github
  to   = aws_iam_openid_connect_provider.github[0]
}

data "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 0 : 1
  url   = "https://token.actions.githubusercontent.com"
}

locals {
  # Exactly one of the two exists.
  github_oidc_provider_arn = one(concat(
    aws_iam_openid_connect_provider.github[*].arn,
    data.aws_iam_openid_connect_provider.github[*].arn,
  ))
}

data "aws_iam_policy_document" "github_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["${var.github_oidc_subject_prefix}:environment:${var.github_environment}"]
    }
  }
}

resource "aws_iam_role" "deploy" {
  name               = "docflow-${var.environment}-github-deploy"
  assume_role_policy = data.aws_iam_policy_document.github_assume.json
}

data "aws_iam_policy_document" "deploy" {
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # no resource-level scoping for this action
  }
  statement {
    sid = "PushImages"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [aws_ecr_repository.app.arn]
  }
  statement {
    sid       = "UploadDeployBundle"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.documents.arn}/deploy/*"]
  }
  statement {
    sid     = "RunDeployOnTheServer"
    actions = ["ssm:SendCommand"]
    resources = [
      aws_instance.app.arn,
      "arn:aws:ssm:${var.region}::document/AWS-RunShellScript",
    ]
  }
  statement {
    sid       = "FollowTheDeploy"
    actions   = ["ssm:GetCommandInvocation", "ssm:ListCommandInvocations"]
    resources = ["*"] # these actions have no resource-level scoping
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "docflow-deploy"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}
