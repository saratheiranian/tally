resource "aws_sns_topic" "alarms" {
  name              = "${local.name}-alarms"
  kms_master_key_id = "alias/aws/sns"
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alarm_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

locals {
  alarm_actions = [aws_sns_topic.alarms.arn]
}

# --- Data integrity ---------------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name          = "${local.name}-dlq-not-empty"
  alarm_description   = "Messages failed 5 deliveries and were dead-lettered. Events are NOT lost, but they are not counted until someone inspects and redrives them."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dlq.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "commit_failures" {
  alarm_name          = "${local.name}-commit-failures"
  alarm_description   = "Workers are failing to commit billing/sketch transactions (Postgres trouble). Messages will retry, but stats are going stale."
  namespace           = "Tally"
  metric_name         = "CommitFailures"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 5
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
}

# --- Freshness ----------------------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "pipeline_stale" {
  for_each            = toset(local.queue_names)
  alarm_name          = "${each.value}-stale"
  alarm_description   = "Events on ${each.value} have waited > 5 minutes even after autoscaling. Stats are stale."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  dimensions          = { QueueName = each.value }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  comparison_operator = "GreaterThanThreshold"
  threshold           = 300
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

# --- API health ------------------------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "api_5xx_rate" {
  alarm_name          = "${local.name}-api-5xx-rate"
  alarm_description   = "More than 1% of API requests are failing with 5xx."
  comparison_operator = "GreaterThanThreshold"
  threshold           = 1
  evaluation_periods  = 3
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions

  metric_query {
    id          = "rate"
    expression  = "100 * errors / MAX([requests, 1])"
    label       = "5xx %"
    return_data = true
  }
  metric_query {
    id = "errors"
    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "HTTPCode_Target_5XX_Count"
      dimensions  = { LoadBalancer = aws_lb.main.arn_suffix }
      period      = 60
      stat        = "Sum"
    }
  }
  metric_query {
    id = "requests"
    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "RequestCount"
      dimensions  = { LoadBalancer = aws_lb.main.arn_suffix }
      period      = 60
      stat        = "Sum"
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "api_p99_latency" {
  alarm_name          = "${local.name}-api-p99-latency"
  alarm_description   = "p99 API latency above 500 ms for 5 minutes."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "TargetResponseTime"
  dimensions          = { LoadBalancer = aws_lb.main.arn_suffix }
  extended_statistic  = "p99"
  period              = 60
  evaluation_periods  = 5
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0.5
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "api_no_healthy_hosts" {
  alarm_name          = "${local.name}-api-no-healthy-hosts"
  alarm_description   = "The load balancer has no healthy API tasks: ingestion is down."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HealthyHostCount"
  dimensions          = { LoadBalancer = aws_lb.main.arn_suffix, TargetGroup = aws_lb_target_group.api.arn_suffix }
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 2
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  treat_missing_data  = local.bootstrap ? "notBreaching" : "breaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

# --- Database ---------------------------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "rds_cpu" {
  alarm_name          = "${local.name}-rds-cpu"
  alarm_description   = "RDS CPU above 80% for 10 minutes."
  namespace           = "AWS/RDS"
  metric_name         = "CPUUtilization"
  dimensions          = { DBInstanceIdentifier = aws_db_instance.main.identifier }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  comparison_operator = "GreaterThanThreshold"
  threshold           = 80
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "rds_storage" {
  alarm_name          = "${local.name}-rds-free-storage"
  alarm_description   = "Less than 2 GiB free on RDS."
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  dimensions          = { DBInstanceIdentifier = aws_db_instance.main.identifier }
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "LessThanThreshold"
  threshold           = 2147483648
  alarm_actions       = local.alarm_actions
}

# --- Dashboard -----------------------------------------------------------------------
locals {
  lb = aws_lb.main.arn_suffix
  dashboard_widgets = [
    { title = "API requests & 5xx", metrics = [
      ["AWS/ApplicationELB", "RequestCount", "LoadBalancer", local.lb, { stat = "Sum" }],
      [".", "HTTPCode_Target_5XX_Count", ".", ".", { stat = "Sum", yAxis = "right" }],
    ] },
    { title = "API latency (s)", metrics = [
      ["AWS/ApplicationELB", "TargetResponseTime", "LoadBalancer", local.lb, { stat = "p50" }],
      ["...", { stat = "p99" }],
    ] },
    { title = "Queue backlog (visible messages)", metrics = [
      for q in local.queue_names : ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", q]
    ] },
    { title = "Oldest message age (s): stats freshness", metrics = [
      for q in local.queue_names : ["AWS/SQS", "ApproximateAgeOfOldestMessage", "QueueName", q, { stat = "Maximum" }]
    ] },
    { title = "Events billed / retries (worker EMF)", metrics = [
      ["Tally", "EventsBilled", { stat = "Sum" }],
      [".", "MessagesRetried", { stat = "Sum", yAxis = "right" }],
      [".", "MessagesAlreadyApplied", { stat = "Sum", yAxis = "right" }],
      [".", "CommitFailures", { stat = "Sum", yAxis = "right" }],
    ] },
    { title = "Commit latency (ms)", metrics = [
      ["Tally", "CommitLatency", { stat = "p50" }],
      ["...", { stat = "p99" }],
    ] },
    { title = "Dead-letter queue", metrics = [
      ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.dlq.name, { stat = "Maximum" }],
    ] },
    { title = "DynamoDB writes & throttles", metrics = [
      ["AWS/DynamoDB", "ConsumedWriteCapacityUnits", "TableName", aws_dynamodb_table.events.name, { stat = "Sum" }],
      [".", "WriteThrottleEvents", ".", ".", { stat = "Sum", yAxis = "right" }],
    ] },
    { title = "RDS CPU % & connections", metrics = [
      ["AWS/RDS", "CPUUtilization", "DBInstanceIdentifier", aws_db_instance.main.identifier],
      [".", "DatabaseConnections", ".", ".", { yAxis = "right" }],
    ] },
    { title = "ECS running tasks", metrics = concat(
      [["ECS/ContainerInsights", "RunningTaskCount", "ClusterName", aws_ecs_cluster.main.name, "ServiceName", "api"]],
      [for g in keys(local.worker_groups) : ["ECS/ContainerInsights", "RunningTaskCount", "ClusterName", aws_ecs_cluster.main.name, "ServiceName", "worker-${g}"]],
    ) },
  ]
}

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = local.name
  dashboard_body = jsonencode({
    widgets = [for i, w in local.dashboard_widgets : {
      type   = "metric"
      x      = (i % 2) * 12
      y      = floor(i / 2) * 6
      width  = 12
      height = 6
      properties = {
        title   = w.title
        region  = var.region
        view    = "timeSeries"
        stacked = false
        period  = 60
        metrics = w.metrics
      }
    }]
  })
}
