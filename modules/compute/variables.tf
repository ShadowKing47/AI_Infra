variable "project_name" {
  type = string
}

variable "environment" {
  type = string
}

variable "common_tags" {
  type    = map(string)
  default = {}
}

# ── Identity ───────────────────────────────────────────────────────────────────

variable "tier_name" {
  type        = string
  description = "Short label for this ASG tier — used in resource names (e.g. 'web', 'ml')."
}

# ── Networking ─────────────────────────────────────────────────────────────────

variable "subnet_ids" {
  type        = list(string)
  description = "Private subnet IDs the ASG launches instances into."
}

variable "security_group_ids" {
  type        = list(string)
  description = "Security groups attached to every instance in this ASG."
}

# ── Instance ───────────────────────────────────────────────────────────────────

variable "instance_type" {
  type        = string
  description = "EC2 instance type (e.g. t3.micro for web, c5.xlarge for ML inference)."
  default     = "t3.micro"
}

variable "ami_id" {
  type        = string
  description = "AMI ID. Use Amazon Linux 2023 in the target region."
}

variable "user_data" {
  type        = string
  description = "Base64-encoded user-data script run on first boot."
  default     = ""
}

variable "iam_instance_profile_arn" {
  type        = string
  description = "ARN of the IAM instance profile to attach. Leave empty to create a minimal default."
  default     = ""
}

# ── Scaling ────────────────────────────────────────────────────────────────────

variable "min_size" {
  type    = number
  default = 1
}

variable "max_size" {
  type    = number
  default = 4
}

variable "desired_capacity" {
  type    = number
  default = 1
}

variable "health_check_grace_period" {
  type        = number
  description = "Seconds ASG waits after launch before checking health. Set high enough for app + model load."
  default     = 120
}

variable "health_check_type" {
  type        = string
  description = "ELB (recommended when behind ALB) or EC2."
  default     = "ELB"
  validation {
    condition     = contains(["ELB", "EC2"], var.health_check_type)
    error_message = "health_check_type must be ELB or EC2."
  }
}

variable "target_group_arns" {
  type        = list(string)
  description = "ALB target group ARNs this ASG registers instances into."
  default     = []
}

# ── Warm pool ──────────────────────────────────────────────────────────────────

variable "enable_warm_pool" {
  type        = bool
  description = "Pre-warm instances so scale-out is instant. Recommended for ML inference tier."
  default     = false
}

variable "warm_pool_min_size" {
  type    = number
  default = 1
}
