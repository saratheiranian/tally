# ---------------------------------------------------------------------------
# RDS Postgres: tenants, keys, billing ledger, sketches.
# The master password is generated and rotated by RDS in Secrets Manager
# (manage_master_user_password), so it never appears in Terraform state or code.
# ---------------------------------------------------------------------------
resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_parameter_group" "main" {
  name   = "${local.name}-pg16"
  family = "postgres16"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  parameter {
    name  = "log_min_duration_statement"
    value = "500" # log queries slower than 500 ms
  }
}

resource "aws_db_instance" "main" {
  #checkov:skip=CKV_AWS_293:Deletion protection is var.deletion_protection so demo stacks can be destroyed; true in production
  #checkov:skip=CKV_AWS_157:Multi-AZ is var.db_multi_az (doubles cost); true in production
  #checkov:skip=CKV_AWS_353:Performance Insights left off for the demo instance class; enable when sizing up
  #checkov:skip=CKV_AWS_118:Enhanced monitoring adds a role and per-instance cost; CloudWatch basics plus slow-query logs cover the demo
  identifier     = local.name
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.db_instance_class

  db_name                     = "tally"
  username                    = "tally"
  manage_master_user_password = true
  # Lets tasks connect with short-lived IAM tokens instead of the password later.
  iam_database_authentication_enabled = true

  allocated_storage     = 20
  max_allocated_storage = 100 # storage autoscaling
  storage_type          = "gp3"
  storage_encrypted     = true

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  parameter_group_name   = aws_db_parameter_group.main.name
  publicly_accessible    = false
  multi_az               = var.db_multi_az

  backup_retention_period         = 7
  copy_tags_to_snapshot           = true
  auto_minor_version_upgrade      = true
  enabled_cloudwatch_logs_exports = ["postgresql"]
  monitoring_interval             = 0

  deletion_protection       = var.deletion_protection
  skip_final_snapshot       = !var.deletion_protection
  final_snapshot_identifier = var.deletion_protection ? "${local.name}-final" : null
  apply_immediately         = !var.deletion_protection
}

# ---------------------------------------------------------------------------
# ElastiCache Redis: the distributed rate limiter's token buckets.
# ---------------------------------------------------------------------------
resource "aws_elasticache_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_elasticache_replication_group" "main" {
  #checkov:skip=CKV2_AWS_50:Rate-limiter state is disposable: losing the node only refills token buckets, so failover is not worth 2x cost
  #checkov:skip=CKV_AWS_31:TLS in transit is on and only the API security group can connect; an AUTH token is a follow-up
  #checkov:skip=CKV_AWS_191:AWS-managed encryption at rest is on; a customer-managed KMS key adds cost and key admin for a demo (see infra/README.md#security)
  replication_group_id = local.name
  description          = "Tally rate limiter"
  engine               = "redis"
  engine_version       = "7.1"
  node_type            = var.redis_node_type
  num_cache_clusters   = 1
  port                 = 6379

  subnet_group_name          = aws_elasticache_subnet_group.main.name
  security_group_ids         = [aws_security_group.redis.id]
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true # clients use rediss://
  automatic_failover_enabled = false
  auto_minor_version_upgrade = true
  apply_immediately          = true
}

# ---------------------------------------------------------------------------
# SQS: one queue per shard, all redriving to a shared dead-letter queue.
# ---------------------------------------------------------------------------
resource "aws_sqs_queue" "dlq" {
  name                      = "${local.name}-events-dlq"
  message_retention_seconds = 1209600 # 14 days, the maximum: time to investigate
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "shard" {
  for_each = toset(local.queue_names)

  name                       = each.value
  visibility_timeout_seconds = 60
  message_retention_seconds  = 345600 # 4 days
  receive_wait_time_seconds  = 20     # long polling
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 5
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [for q in aws_sqs_queue.shard : q.arn]
  })
}

# ---------------------------------------------------------------------------
# DynamoDB: raw events. Key design is explained in services/backend/app/store.py.
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "events" {
  #checkov:skip=CKV_AWS_119:AWS-managed encryption at rest is on; a customer-managed KMS key adds cost and key admin for a demo (see infra/README.md#security)
  name         = "${local.name}-events"
  billing_mode = "PAY_PER_REQUEST" # spiky ingest; no capacity planning
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }
  server_side_encryption {
    enabled = true # AWS-managed KMS key
  }
  deletion_protection_enabled = var.deletion_protection
}

# ---------------------------------------------------------------------------
# ECR: backend image (API, worker and migration runner share one image).
# ---------------------------------------------------------------------------
resource "aws_ecr_repository" "backend" {
  name                 = "${local.name}-backend"
  image_tag_mutability = "IMMUTABLE" # a tag always means the same bits
  force_delete         = !var.deletion_protection

  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "KMS"
  }
}

resource "aws_ecr_lifecycle_policy" "backend" {
  repository = aws_ecr_repository.backend.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 20 most recent images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 20 }
      action       = { type = "expire" }
    }]
  })
}
