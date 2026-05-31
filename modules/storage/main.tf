locals {
  # Normalise the bucket map so lifecycle rules are only added when needed.
  buckets = var.buckets
}

resource "aws_s3_bucket" "this" {
  for_each = local.buckets

  bucket        = "${var.project_name}-${var.environment}-${each.key}"
  force_destroy = each.value.force_destroy

  tags = merge(var.common_tags, {
    Name    = "${var.project_name}-${var.environment}-${each.key}"
    Purpose = each.key
  })
}

resource "aws_s3_bucket_versioning" "this" {
  for_each = { for k, v in local.buckets : k => v if v.versioning }

  bucket = aws_s3_bucket.this[each.key].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  for_each = local.buckets

  bucket = aws_s3_bucket.this[each.key].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "this" {
  for_each = local.buckets

  bucket                  = aws_s3_bucket.this[each.key].id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "this" {
  # Only create a lifecycle rule when at least one of glacier_days / expiry_days is set.
  for_each = {
    for k, v in local.buckets : k => v
    if v.glacier_days > 0 || v.expiry_days > 0
  }

  bucket = aws_s3_bucket.this[each.key].id

  rule {
    id     = "lifecycle"
    status = "Enabled"

    filter {} # applies to all objects

    dynamic "transition" {
      for_each = each.value.glacier_days > 0 ? [each.value.glacier_days] : []
      content {
        days          = transition.value
        storage_class = "GLACIER"
      }
    }

    dynamic "expiration" {
      for_each = each.value.expiry_days > 0 ? [each.value.expiry_days] : []
      content {
        days = expiration.value
      }
    }
  }
}

# ALB requires a bucket policy granting the ELB service account write access
# before it can deliver access logs. This policy is attached to any bucket
# whose key ends in "-logs" or is named "alb-logs".
data "aws_elb_service_account" "main" {}

resource "aws_s3_bucket_policy" "alb_logs" {
  for_each = { for k, v in local.buckets : k => v if k == "alb-logs" }

  bucket = aws_s3_bucket.this[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowALBLogDelivery"
      Effect    = "Allow"
      Principal = { AWS = data.aws_elb_service_account.main.arn }
      Action    = "s3:PutObject"
      Resource  = "${aws_s3_bucket.this[each.key].arn}/alb-logs/AWSLogs/*"
    }]
  })
}
