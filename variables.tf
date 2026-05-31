variable "aws_region" {
  type        = string
  description = "AWS region to deploy into."
  default     = "us-east-1"
}

variable "project_name" {
  type        = string
  description = "Short identifier used in all resource names (e.g. 'ai-infra')."
  default     = "ai-infra"
}

variable "environment" {
  type        = string
  description = "Deployment environment: dev | staging | prod."
  default     = "dev"
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

# ── Networking ─────────────────────────────────────────────────────────────────

variable "vpc_cidr" {
  type        = string
  description = "VPC CIDR. /16 recommended — leaves room for all subnet tiers."
  default     = "10.0.0.0/16"
}

variable "azs" {
  type        = list(string)
  description = "Availability zones to deploy into. Minimum 2 for HA."
  default     = ["us-east-1a", "us-east-1b"]
}

variable "enable_nat_gateway" {
  type        = bool
  description = "Create NAT Gateways for private subnet egress. Set false to save cost in isolated dev environments."
  default     = true
}

variable "single_nat_gateway" {
  type        = bool
  description = "Use a single NAT Gateway instead of one per AZ. Not HA — dev only."
  default     = false
}
