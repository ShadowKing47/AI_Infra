output "asg_name" {
  description = "Name of the Auto Scaling Group."
  value       = aws_autoscaling_group.this.name
}

output "asg_arn" {
  description = "ARN of the Auto Scaling Group."
  value       = aws_autoscaling_group.this.arn
}

output "launch_template_id" {
  description = "ID of the Launch Template."
  value       = aws_launch_template.this.id
}

output "iam_role_arn" {
  description = "ARN of the EC2 IAM role (empty if a custom profile was supplied)."
  value       = local.create_default_profile ? aws_iam_role.default[0].arn : ""
}

output "iam_instance_profile_arn" {
  description = "ARN of the instance profile in use."
  value       = local.resolved_profile_arn
}
