resource "aws_ecs_cluster" "main" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_cloudwatch_log_group" "api" {
  #checkov:skip=CKV_AWS_158:AWS-managed encryption at rest is on; a customer-managed KMS key adds cost and key admin for a demo (see infra/README.md#security)
  #checkov:skip=CKV_AWS_338:Retention is var.log_retention_days (30 by default) to keep demo costs down; set 365 for production
  name              = "/${var.project}/${var.environment}/api"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "worker" {
  #checkov:skip=CKV_AWS_158:AWS-managed encryption at rest is on; a customer-managed KMS key adds cost and key admin for a demo (see infra/README.md#security)
  #checkov:skip=CKV_AWS_338:Retention is var.log_retention_days (30 by default) to keep demo costs down; set 365 for production
  for_each          = local.worker_groups
  name              = "/${var.project}/${var.environment}/worker-${each.key}"
  retention_in_days = var.log_retention_days
}

locals {
  app_environment = [
    { name = "TALLY_SINK", value = "sqs" },
    { name = "TALLY_AWS_REGION", value = var.region },
    { name = "TALLY_DB_HOST", value = aws_db_instance.main.address },
    { name = "TALLY_DB_NAME", value = aws_db_instance.main.db_name },
    { name = "TALLY_DB_USER", value = aws_db_instance.main.username },
    { name = "TALLY_REDIS_URL", value = "rediss://${aws_elasticache_replication_group.main.primary_endpoint_address}:6379/0" },
    { name = "TALLY_QUEUE_PREFIX", value = "${local.name}-events" },
    { name = "TALLY_QUEUE_SHARDS", value = tostring(var.queue_shards) },
    { name = "TALLY_DLQ_NAME", value = aws_sqs_queue.dlq.name },
    { name = "TALLY_DYNAMODB_TABLE", value = aws_dynamodb_table.events.name },
  ]
  app_secrets = [
    # RDS stores {"username": ..., "password": ...}; ECS extracts one JSON key.
    { name = "TALLY_DB_PASSWORD", valueFrom = "${aws_db_instance.main.master_user_secret[0].secret_arn}:password::" },
  ]

  # Init container: runs migrations before the app container starts, from the
  # same image. The advisory lock in app/migrate.py makes concurrent task starts
  # safe; when the schema is current it is a no-op. See ADR 0005.
  migrate_container = {
    name                   = "migrate"
    image                  = local.image
    essential              = false
    command                = ["python", "-m", "app.migrate"]
    environment            = local.app_environment
    secrets                = local.app_secrets
    readonlyRootFilesystem = true
  }

  log_config = { for k, g in merge({ api = aws_cloudwatch_log_group.api }, aws_cloudwatch_log_group.worker) : k => {
    logDriver = "awslogs"
    options = {
      awslogs-group         = g.name
      awslogs-region        = var.region
      awslogs-stream-prefix = "ecs"
      mode                  = "non-blocking" # never let logging stall the app
      max-buffer-size       = "4m"
    }
  } }
}

# ---------------------------------------------------------------------------
# API service
# ---------------------------------------------------------------------------
resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.api.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    merge(local.migrate_container, { logConfiguration = local.log_config["api"] }),
    {
      name      = "api"
      image     = local.image
      essential = true
      # One process per task: scale with tasks (visible to ECS and the ALB), not
      # hidden worker processes.
      command                = ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
      portMappings           = [{ containerPort = 8000, protocol = "tcp" }]
      environment            = local.app_environment
      secrets                = local.app_secrets
      readonlyRootFilesystem = true
      dependsOn              = [{ containerName = "migrate", condition = "SUCCESS" }]
      logConfiguration       = local.log_config["api"]
    },
  ])
}

resource "aws_ecs_service" "api" {
  name                              = "api"
  cluster                           = aws_ecs_cluster.main.id
  task_definition                   = aws_ecs_task_definition.api.arn
  desired_count                     = local.bootstrap ? 0 : var.api_desired_count
  launch_type                       = "FARGATE"
  health_check_grace_period_seconds = 60
  propagate_tags                    = "SERVICE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.api.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  deployment_circuit_breaker {
    enable   = true
    rollback = true # a deploy whose tasks fail health checks rolls itself back
  }

  lifecycle {
    ignore_changes = [desired_count] # owned by autoscaling after creation
  }
  depends_on = [aws_lb_listener.http]
}

# ---------------------------------------------------------------------------
# Worker services: one per shard group
# ---------------------------------------------------------------------------
resource "aws_ecs_task_definition" "worker" {
  for_each                 = local.worker_groups
  family                   = "${local.name}-worker-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.worker[each.key].arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    merge(local.migrate_container, { logConfiguration = local.log_config[each.key] }),
    {
      name      = "worker"
      image     = local.image
      essential = true
      command   = ["python", "-m", "app.worker"]
      environment = concat(local.app_environment, [
        { name = "TALLY_WORKER_SHARDS", value = join(",", [for s in each.value : tostring(s)]) },
        { name = "TALLY_WORKER_WAIT_SECONDS", value = "20" },
      ])
      secrets                = local.app_secrets
      readonlyRootFilesystem = true
      # SIGTERM -> stop polling -> finish in-flight messages. Must exceed one
      # long-poll (20 s) plus a commit.
      stopTimeout      = 45
      dependsOn        = [{ containerName = "migrate", condition = "SUCCESS" }]
      logConfiguration = local.log_config[each.key]
    },
  ])
}

resource "aws_ecs_service" "worker" {
  for_each        = local.worker_groups
  name            = "worker-${each.key}"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker[each.key].arn
  desired_count   = local.bootstrap ? 0 : 1
  launch_type     = "FARGATE"
  propagate_tags  = "SERVICE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.worker.id]
    assign_public_ip = false
  }

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  lifecycle {
    ignore_changes = [desired_count]
  }
}

# ---------------------------------------------------------------------------
# Load balancer
# ---------------------------------------------------------------------------
resource "aws_lb" "main" {
  #checkov:skip=CKV_AWS_150:Deletion protection is var.deletion_protection so demo stacks can be destroyed
  #checkov:skip=CKV_AWS_91:Access logs need an S3 bucket plus lifecycle policy; out of scope for the demo (see infra/README.md#security)
  #checkov:skip=CKV2_AWS_28:WAF costs about $5/month plus per-rule fees; per-tenant rate limiting is enforced in the app (Redis token bucket)
  #checkov:skip=CKV_AWS_2:HTTPS is enabled by setting var.acm_certificate_arn (needs a domain); HTTP then redirects to it
  name                       = local.name
  load_balancer_type         = "application"
  internal                   = false
  subnets                    = aws_subnet.public[*].id
  security_groups            = [aws_security_group.alb.id]
  drop_invalid_header_fields = true
  enable_deletion_protection = var.deletion_protection
}

resource "aws_lb_target_group" "api" {
  #checkov:skip=CKV_AWS_378:TLS terminates at the ALB; traffic to targets stays inside the VPC
  name                 = "${local.name}-api"
  port                 = 8000
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = aws_vpc.main.id
  deregistration_delay = 30

  health_check {
    path                = "/readyz" # checks Postgres, Redis and SQS reachability
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

locals {
  https = var.acm_certificate_arn != ""
}

resource "aws_lb_listener" "http" {
  #checkov:skip=CKV_AWS_2:Redirects to HTTPS when var.acm_certificate_arn is set; plain HTTP only for domain-less demos
  #checkov:skip=CKV_AWS_378:As above
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = local.https ? "redirect" : "forward"
    target_group_arn = local.https ? null : aws_lb_target_group.api.arn

    dynamic "redirect" {
      for_each = local.https ? [1] : []
      content {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }
}

resource "aws_lb_listener" "https" {
  count             = local.https ? 1 : 0
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.acm_certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}
