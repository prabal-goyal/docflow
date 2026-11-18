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

variable "github_repository" {
  description = "owner/name of the GitHub repository allowed to deploy (from its main branch only)."
  type        = string
  default     = "prabal-goyal/docflow"
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
