# API: track CPU and requests per task; whichever needs more capacity wins.
resource "aws_appautoscaling_target" "api" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.api.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = local.bootstrap ? 0 : var.api_desired_count
  max_capacity       = local.bootstrap ? 0 : var.api_max_count
}

resource "aws_appautoscaling_policy" "api_cpu" {
  name               = "cpu-60"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.api.service_namespace
  resource_id        = aws_appautoscaling_target.api.resource_id
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension

  target_tracking_scaling_policy_configuration {
    target_value       = 60
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}

resource "aws_appautoscaling_policy" "api_requests" {
  name               = "requests-per-task"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.api.service_namespace
  resource_id        = aws_appautoscaling_target.api.resource_id
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension

  target_tracking_scaling_policy_configuration {
    target_value       = 3000 # requests per task per minute (~50 rps); tune from load tests
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
    predefined_metric_specification {
      predefined_metric_type = "ALBRequestCountPerTarget"
      resource_label         = "${aws_lb.main.arn_suffix}/${aws_lb_target_group.api.arn_suffix}"
    }
  }
}

# Workers: scale on *latency* (age of the oldest waiting message), not CPU.
# Backlog age is what users feel ("how stale are my stats?"), and it's
# independent of message size. Extra tasks on one shard group are safe:
# sketch rows are lock-protected (ADR 0004).
resource "aws_appautoscaling_target" "worker" {
  for_each           = local.worker_groups
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.worker[each.key].name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = local.bootstrap ? 0 : 1
  max_capacity       = local.bootstrap ? 0 : var.worker_max_count
}

resource "aws_appautoscaling_policy" "worker_out" {
  for_each           = local.worker_groups
  name               = "backlog-scale-out"
  policy_type        = "StepScaling"
  service_namespace  = "ecs"
  resource_id        = aws_appautoscaling_target.worker[each.key].resource_id
  scalable_dimension = aws_appautoscaling_target.worker[each.key].scalable_dimension

  step_scaling_policy_configuration {
    adjustment_type         = "ChangeInCapacity"
    cooldown                = 120
    metric_aggregation_type = "Maximum"
    step_adjustment {
      metric_interval_lower_bound = 0
      metric_interval_upper_bound = 120
      scaling_adjustment          = 1
    }
    step_adjustment {
      metric_interval_lower_bound = 120 # badly behind: add two at once
      scaling_adjustment          = 2
    }
  }
}

resource "aws_appautoscaling_policy" "worker_in" {
  for_each           = local.worker_groups
  name               = "backlog-scale-in"
  policy_type        = "StepScaling"
  service_namespace  = "ecs"
  resource_id        = aws_appautoscaling_target.worker[each.key].resource_id
  scalable_dimension = aws_appautoscaling_target.worker[each.key].scalable_dimension

  step_scaling_policy_configuration {
    adjustment_type         = "ChangeInCapacity"
    cooldown                = 300
    metric_aggregation_type = "Maximum"
    step_adjustment {
      metric_interval_upper_bound = 0
      scaling_adjustment          = -1
    }
  }
}

# Oldest-message age across the group's shards: MAX over one metric per queue.
resource "aws_cloudwatch_metric_alarm" "worker_backlog_high" {
  for_each            = local.worker_groups
  alarm_name          = "${local.name}-worker-${each.key}-backlog-high"
  alarm_description   = "Oldest message on shards ${join(",", [for s in each.value : tostring(s)])} is > 30s old: scale out."
  comparison_operator = "GreaterThanThreshold"
  threshold           = 30
  evaluation_periods  = 2
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_appautoscaling_policy.worker_out[each.key].arn]

  metric_query {
    id          = "oldest"
    expression  = "MAX([${join(",", [for s in each.value : "q${s}"])}])"
    label       = "Oldest message age (s)"
    return_data = true
  }
  dynamic "metric_query" {
    for_each = each.value
    content {
      id = "q${metric_query.value}"
      metric {
        namespace   = "AWS/SQS"
        metric_name = "ApproximateAgeOfOldestMessage"
        period      = 60
        stat        = "Maximum"
        dimensions  = { QueueName = local.queue_names[metric_query.value] }
      }
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "worker_backlog_low" {
  for_each            = local.worker_groups
  alarm_name          = "${local.name}-worker-${each.key}-backlog-low"
  alarm_description   = "Shards ${join(",", [for s in each.value : tostring(s)])} are caught up: scale in."
  comparison_operator = "LessThanThreshold"
  threshold           = 5
  evaluation_periods  = 15 # 15 calm minutes before removing capacity
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_appautoscaling_policy.worker_in[each.key].arn]

  metric_query {
    id          = "oldest"
    expression  = "MAX([${join(",", [for s in each.value : "q${s}"])}])"
    return_data = true
  }
  dynamic "metric_query" {
    for_each = each.value
    content {
      id = "q${metric_query.value}"
      metric {
        namespace   = "AWS/SQS"
        metric_name = "ApproximateAgeOfOldestMessage"
        period      = 60
        stat        = "Maximum"
        dimensions  = { QueueName = local.queue_names[metric_query.value] }
      }
    }
  }
}
