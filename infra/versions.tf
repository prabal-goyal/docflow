terraform {
  required_version = ">= 1.17"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.72"
    }
  }

  # State is local (gitignored). It holds resource ids, not secrets: secret values are written to
  # SSM with the AWS CLI, never through Terraform. Upgrade path for a team: an S3 backend with
  # use_lockfile = true.
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile

  default_tags {
    tags = {
      Project     = "docflow"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {
  lifecycle {
    # Each environment keeps its state in its own workspace ("default" holds prod). This stops a
    # plan that would apply one environment's variables to another environment's state.
    precondition {
      condition     = var.environment == (terraform.workspace == "default" ? "prod" : terraform.workspace)
      error_message = "Workspace and environment differ: select workspace \"${var.environment == "prod" ? "default" : var.environment}\" (terraform workspace select ...) or pass the matching -var environment=..."
    }
  }
}
