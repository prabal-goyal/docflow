variable "region" {
  description = "AWS region. Mumbai keeps data in India, which Indian enterprise customers often require."
  type        = string
  default     = "ap-south-1"
}

variable "aws_profile" {
  description = "AWS CLI profile used by Terraform (IAM Identity Center login)."
  type        = string
  default     = "docflow"
}

variable "environment" {
  description = "Environment name; also the SSM parameter prefix (/docflow/<environment>/...)."
  type        = string
  default     = "prod"
}

variable "github_oidc_subject_prefix" {
  description = <<-EOT
    Start of the OIDC subject GitHub issues for the deploying repository. Repositories using
    GitHub's immutable subjects include the owner and repository ids, so a deleted-and-recreated
    repository with the same name cannot deploy. Look it up with:
      gh api repos/OWNER/REPO/actions/oidc/customization/sub   (field sub_claim_prefix)
  EOT
  type        = string
  default     = "repo:prabal-goyal@89393467/docflow@1461059601"
}

variable "instance_type" {
  description = "EC2 instance type. t3.small (2 vCPU, 2 GiB) fits the whole stack with swap."
  type        = string
  default     = "t3.small"
}

variable "root_volume_gb" {
  description = "Root EBS volume size in GiB (Docker images, Postgres data, swap)."
  type        = number
  default     = 20
}

variable "backup_retention_days" {
  description = "Days to keep nightly database backups in S3."
  type        = number
  default     = 14
}

variable "log_retention_days" {
  description = "Days to keep CloudWatch logs."
  type        = number
  default     = 14
}

variable "github_environment" {
  description = "GitHub environment whose deploy jobs may assume the deploy role."
  type        = string
  default     = "production"
}

variable "create_github_oidc_provider" {
  description = "Create the account's GitHub OIDC provider. Set false for every environment after the first in an account."
  type        = bool
  default     = true
}
