variable "project" {
  type    = string
  default = "tally"
}

variable "environment" {
  type    = string
  default = "demo"
}

variable "region" {
  type    = string
  default = "eu-west-2"
}

variable "az_count" {
  description = "Availability zones to span (ALB and RDS subnet groups need at least 2)."
  type        = number
  default     = 2
  validation {
    condition     = var.az_count >= 2 && var.az_count <= 3
    error_message = "az_count must be 2 or 3."
  }
}

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "single_nat_gateway" {
  description = "One NAT gateway for all AZs (cheaper; an AZ outage cuts egress). Set false for one per AZ."
  type        = bool
  default     = true
}

variable "image_tag" {
  description = <<-EOT
    Backend image tag in ECR (CI passes the git SHA). Leave empty on the very first
    apply: everything is created but services run 0 tasks until an image exists.
  EOT
  type        = string
  default     = ""
}

variable "queue_shards" {
  type    = number
  default = 4
}

variable "worker_shard_groups" {
  description = "Each entry is one ECS worker service and the shard indexes it consumes, e.g. [\"0,1\", \"2,3\"]."
  type        = list(string)
  default     = ["0,1", "2,3"]
  validation {
    condition = (
      length(distinct(flatten([for g in var.worker_shard_groups : split(",", g)]))) ==
      length(flatten([for g in var.worker_shard_groups : split(",", g)]))
    )
    error_message = "A shard may appear in only one worker group."
  }
}

variable "api_desired_count" {
  type    = number
  default = 2
}

variable "api_max_count" {
  type    = number
  default = 6
}

variable "worker_max_count" {
  description = "Max tasks per worker group. >1 is safe: sketch rows are lock-protected (ADR 0004)."
  type        = number
  default     = 3
}

variable "api_cpu" {
  type    = number
  default = 512
}

variable "api_memory" {
  type    = number
  default = 1024
}

variable "worker_cpu" {
  type    = number
  default = 512
}

variable "worker_memory" {
  type    = number
  default = 1024
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "db_multi_az" {
  type    = bool
  default = false
}

variable "redis_node_type" {
  type    = string
  default = "cache.t4g.micro"
}

variable "deletion_protection" {
  description = "Protect RDS and DynamoDB from deletion. false for demo stacks you intend to destroy."
  type        = bool
  default     = false
}

variable "allowed_ingress_cidrs" {
  description = "Who may reach the load balancer."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "acm_certificate_arn" {
  description = "If set, the ALB serves HTTPS on 443 and redirects HTTP to it."
  type        = string
  default     = ""
}

variable "alarm_email" {
  description = "Email for CloudWatch alarm notifications (confirm the subscription email). Empty = no email."
  type        = string
  default     = ""
}

variable "log_retention_days" {
  type    = number
  default = 30
}
