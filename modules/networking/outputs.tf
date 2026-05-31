output "vpc_id" {
  description = "ID of the VPC."
  value       = aws_vpc.this.id
}

output "vpc_cidr" {
  description = "CIDR block of the VPC."
  value       = aws_vpc.this.cidr_block
}

output "public_subnet_ids" {
  description = "List of public subnet IDs (one per AZ)."
  value       = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  description = "List of private (app) subnet IDs (one per AZ)."
  value       = aws_subnet.private[*].id
}

output "database_subnet_ids" {
  description = "List of database subnet IDs (one per AZ)."
  value       = aws_subnet.database[*].id
}

output "nat_gateway_ids" {
  description = "IDs of NAT Gateways created (empty if enable_nat_gateway=false)."
  value       = aws_nat_gateway.this[*].id
}

output "internet_gateway_id" {
  description = "ID of the Internet Gateway."
  value       = aws_internet_gateway.this.id
}

output "sg_alb_id" {
  description = "Security group ID for the ALB."
  value       = aws_security_group.alb.id
}

output "sg_app_id" {
  description = "Security group ID for the app tier."
  value       = aws_security_group.app.id
}

output "sg_ml_id" {
  description = "Security group ID for the ML inference tier."
  value       = aws_security_group.ml.id
}

output "sg_db_id" {
  description = "Security group ID for RDS."
  value       = aws_security_group.db.id
}

output "sg_cache_id" {
  description = "Security group ID for ElastiCache."
  value       = aws_security_group.cache.id
}
