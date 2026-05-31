locals {
  name_prefix = "${var.project_name}-${var.environment}-${var.tier_name}"

  # If no instance profile ARN provided, create a minimal one that allows
  # SSM Session Manager access (so we can shell in without a bastion).
  create_default_profile = var.iam_instance_profile_arn == ""
}

# ── Default IAM role (created only when no profile is supplied) ────────────────

resource "aws_iam_role" "default" {
  count = local.create_default_profile ? 1 : 0

  name = "${local.name_prefix}-ec2-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = merge(var.common_tags, { Name = "${local.name_prefix}-ec2-role" })
}

resource "aws_iam_role_policy_attachment" "ssm" {
  count = local.create_default_profile ? 1 : 0

  role       = aws_iam_role.default[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "default" {
  count = local.create_default_profile ? 1 : 0

  name = "${local.name_prefix}-ec2-profile"
  role = aws_iam_role.default[0].name

  tags = merge(var.common_tags, { Name = "${local.name_prefix}-ec2-profile" })
}

locals {
  resolved_profile_arn = local.create_default_profile ? aws_iam_instance_profile.default[0].arn : var.iam_instance_profile_arn
}

# ── Launch Template ────────────────────────────────────────────────────────────

resource "aws_launch_template" "this" {
  name_prefix   = "${local.name_prefix}-lt-"
  image_id      = var.ami_id
  instance_type = var.instance_type

  iam_instance_profile {
    arn = local.resolved_profile_arn
  }

  network_interfaces {
    associate_public_ip_address = false
    security_groups             = var.security_group_ids
    delete_on_termination       = true
  }

  user_data = var.user_data != "" ? var.user_data : null

  metadata_options {
    # IMDSv2 only — prevents SSRF-based metadata exfiltration
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  monitoring {
    enabled = true
  }

  tag_specifications {
    resource_type = "instance"
    tags = merge(var.common_tags, {
      Name = "${local.name_prefix}-instance"
      Tier = var.tier_name
    })
  }

  tag_specifications {
    resource_type = "volume"
    tags = merge(var.common_tags, { Name = "${local.name_prefix}-volume" })
  }

  lifecycle {
    create_before_destroy = true
  }

  tags = merge(var.common_tags, { Name = "${local.name_prefix}-lt" })
}

# ── Auto Scaling Group ─────────────────────────────────────────────────────────

resource "aws_autoscaling_group" "this" {
  name                      = "${local.name_prefix}-asg"
  vpc_zone_identifier       = var.subnet_ids
  min_size                  = var.min_size
  max_size                  = var.max_size
  desired_capacity          = var.desired_capacity
  health_check_type         = var.health_check_type
  health_check_grace_period = var.health_check_grace_period
  target_group_arns         = var.target_group_arns

  launch_template {
    id      = aws_launch_template.this.id
    version = "$Latest"
  }

  # Rolling instance refresh — used when launch template is updated
  instance_refresh {
    strategy = "Rolling"
    preferences {
      min_healthy_percentage = 50
    }
  }

  dynamic "warm_pool" {
    for_each = var.enable_warm_pool ? [1] : []
    content {
      min_size = var.warm_pool_min_size
      pool_state = "Stopped"    # Stopped = fastest scale-out, lowest cost
    }
  }

  tag {
    key                 = "Name"
    value               = "${local.name_prefix}-asg"
    propagate_at_launch = false
  }

  dynamic "tag" {
    for_each = var.common_tags
    content {
      key                 = tag.key
      value               = tag.value
      propagate_at_launch = true
    }
  }

  lifecycle {
    ignore_changes = [desired_capacity] # prevent Terraform from fighting autoscaler
  }
}

# ── Scaling Policies ───────────────────────────────────────────────────────────

# Target-tracking on CPU — sensible default for web tier.
# The ML tier (Phase 4) adds a custom metric policy on top of this.
resource "aws_autoscaling_policy" "cpu" {
  name                   = "${local.name_prefix}-cpu-tracking"
  autoscaling_group_name = aws_autoscaling_group.this.name
  policy_type            = "TargetTrackingScaling"

  target_tracking_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ASGAverageCPUUtilization"
    }
    target_value = 60.0
  }
}
