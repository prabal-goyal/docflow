output "hostname" {
  description = "Public HTTPS hostname (free sslip.io name that resolves to the server's IP)."
  value       = local.hostname
}

output "url" {
  value = "https://${local.hostname}"
}

output "public_ip" {
  value = aws_eip.app.public_ip
}

output "instance_id" {
  value = aws_instance.app.id
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "bucket" {
  value = aws_s3_bucket.documents.bucket
}

output "deploy_role_arn" {
  description = "Role GitHub Actions assumes (set as the AWS_DEPLOY_ROLE_ARN repository variable)."
  value       = aws_iam_role.deploy.arn
}

output "region" {
  value = var.region
}

output "ssm_prefix" {
  value = local.ssm_prefix
}
