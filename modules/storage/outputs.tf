output "bucket_ids" {
  description = "Map of logical name → S3 bucket ID."
  value       = { for k, v in aws_s3_bucket.this : k => v.id }
}

output "bucket_arns" {
  description = "Map of logical name → S3 bucket ARN."
  value       = { for k, v in aws_s3_bucket.this : k => v.arn }
}
