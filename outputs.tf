# ── Phase 1 outputs ───────────────────────────────────────────────────────────

output "vpc_id" {
  description = "VPC ID."
  value       = module.networking.vpc_id
}

output "public_subnet_ids" {
  description = "Public subnet IDs — ALB is deployed here."
  value       = module.networking.public_subnet_ids
}

output "private_subnet_ids" {
  description = "Private subnet IDs — app and ML ASGs are deployed here."
  value       = module.networking.private_subnet_ids
}

output "database_subnet_ids" {
  description = "Database subnet IDs — RDS and ElastiCache are deployed here."
  value       = module.networking.database_subnet_ids
}

output "sg_alb_id" {
  description = "ALB security group ID."
  value       = module.networking.sg_alb_id
}

output "sg_app_id" {
  description = "App tier security group ID."
  value       = module.networking.sg_app_id
}

output "sg_ml_id" {
  description = "ML inference tier security group ID."
  value       = module.networking.sg_ml_id
}

output "sg_db_id" {
  description = "RDS security group ID."
  value       = module.networking.sg_db_id
}

output "sg_cache_id" {
  description = "ElastiCache security group ID."
  value       = module.networking.sg_cache_id
}
