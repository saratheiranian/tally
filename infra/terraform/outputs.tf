output "api_url" {
  description = "Base URL of the ingest/query API."
  value       = "${local.https ? "https" : "http"}://${aws_lb.main.dns_name}"
}

output "ecr_repository_url" {
  value = aws_ecr_repository.backend.repository_url
}

output "ecs_cluster" {
  value = aws_ecs_cluster.main.name
}

output "ecs_services" {
  value = concat([aws_ecs_service.api.name], [for s in aws_ecs_service.worker : s.name])
}

output "dashboard_url" {
  value = "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#dashboards/dashboard/${aws_cloudwatch_dashboard.main.dashboard_name}"
}

output "db_endpoint" {
  value = aws_db_instance.main.address
}

output "db_secret_arn" {
  description = "Secrets Manager ARN holding the RDS master credentials."
  value       = aws_db_instance.main.master_user_secret[0].secret_arn
}

output "private_subnet_ids" {
  description = "For one-off tasks (e.g. `aws ecs run-task` to create a tenant)."
  value       = aws_subnet.private[*].id
}

output "api_security_group_id" {
  value = aws_security_group.api.id
}

output "api_task_definition" {
  value = aws_ecs_task_definition.api.family
}

output "bootstrap_mode" {
  description = "true = no image deployed yet; services run 0 tasks."
  value       = local.bootstrap
}
