# Container images
resource "aws_ecr_repository" "app" {
  name                 = "docflow"
  image_tag_mutability = "IMMUTABLE" # a tag (the git commit) always means the same image

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 20 most recent images (rollback targets)"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 20
      }
      action = { type = "expire" }
    }]
  })
}

# Uploaded invoices, nightly database backups and deployment bundles
resource "aws_s3_bucket" "documents" {
  bucket = "docflow-${var.environment}-${data.aws_caller_identity.current.account_id}-${var.region}"
}

resource "aws_s3_bucket_public_access_block" "documents" {
  bucket                  = aws_s3_bucket.documents.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "documents" {
  bucket = aws_s3_bucket.documents.id
  rule {
    object_ownership = "BucketOwnerEnforced" # no ACLs at all; access is IAM only
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id

  rule {
    id     = "expire-backups"
    status = "Enabled"
    filter {
      prefix = "backups/"
    }
    expiration {
      days = var.backup_retention_days
    }
  }

  rule {
    id     = "expire-deploy-bundles"
    status = "Enabled"
    filter {
      prefix = "deploy/"
    }
    expiration {
      days = 30
    }
  }
}

resource "aws_s3_bucket_policy" "documents" {
  bucket = aws_s3_bucket.documents.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.documents.arn, "${aws_s3_bucket.documents.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.documents]
}

# Encrypts webhook signing secrets in the database (docflow/secret_cipher.py)
resource "aws_kms_key" "webhook_secrets" {
  description             = "DocFlow webhook signing secrets"
  enable_key_rotation     = true
  deletion_window_in_days = 7
}

resource "aws_kms_alias" "webhook_secrets" {
  name          = "alias/docflow-${var.environment}-webhook-secrets"
  target_key_id = aws_kms_key.webhook_secrets.key_id
}

resource "aws_cloudwatch_log_group" "app" {
  name              = "/docflow"
  retention_in_days = var.log_retention_days
}

# Non-secret configuration read by deploy/render-env.sh. Secret values (database password, JWT
# secret, OpenAI key) are created with the AWS CLI so they never enter Terraform state.
locals {
  ssm_prefix = "/docflow/${var.environment}"
  hostname   = "${replace(aws_eip.app.public_ip, ".", "-")}.sslip.io"
}

resource "aws_ssm_parameter" "s3_bucket" {
  name  = "${local.ssm_prefix}/s3-bucket"
  type  = "String"
  value = aws_s3_bucket.documents.bucket
}

resource "aws_ssm_parameter" "kms_key_id" {
  name  = "${local.ssm_prefix}/kms-key-id"
  type  = "String"
  value = aws_kms_alias.webhook_secrets.arn
}

resource "aws_ssm_parameter" "site_address" {
  name  = "${local.ssm_prefix}/site-address"
  type  = "String"
  value = local.hostname
}
