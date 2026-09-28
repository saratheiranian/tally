terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
  }

  # State: local by default so a first `terraform apply` just works.
  # For shared/CI use, copy backend.tf.example to backend.tf (S3 + native locking).
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Repository  = "tally"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_availability_zones" "available" {
  #checkov:skip=CKV_AWS_394:We slice the first var.az_count AZs; new AZs are appended, so the selection is stable
  state = "available"
}

locals {
  name       = "${var.project}-${var.environment}"
  account_id = data.aws_caller_identity.current.account_id
  azs        = slice(data.aws_availability_zones.available.names, 0, var.az_count)

  bootstrap = var.image_tag == "" # no image pushed yet: create everything, run nothing
  image     = "${aws_ecr_repository.backend.repository_url}:${var.image_tag}"

  queue_names = [for i in range(var.queue_shards) : "${local.name}-events-${i}"]

  # worker group key ("0-1") => list of shard indexes it consumes
  worker_groups = { for g in var.worker_shard_groups : replace(g, ",", "-") => [for s in split(",", g) : tonumber(s)] }
}
