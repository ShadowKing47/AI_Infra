variable "project_name" {
  type        = string
  description = "Short project identifier — used in all resource names and tags."
}

variable "environment" {
  type        = string
  description = "Deployment environment (dev / staging / prod)."
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "common_tags" {
  type        = map(string)
  description = "Tags applied to every resource in this module."
  default     = {}
}

variable "vpc_cidr" {
  type        = string
  description = "CIDR block for the VPC. /16 gives 256 /24 subnets."
  default     = "10.0.0.0/16"
}

variable "azs" {
  type        = list(string)
  description = "List of AZs to deploy into. Must have at least 2 for HA."
  validation {
    condition     = length(var.azs) >= 2
    error_message = "At least 2 AZs required for high availability."
  }
}

# Subnet CIDRs are derived from vpc_cidr + newbits in main.tf so callers
# never have to manage CIDR math manually.
variable "public_subnet_newbits" {
  type        = number
  description = "newbits added to vpc_cidr to size public subnets (default 8 → /24)."
  default     = 8
}

variable "private_subnet_newbits" {
  type        = number
  description = "newbits added to vpc_cidr to size private (app) subnets."
  default     = 8
}

variable "database_subnet_newbits" {
  type        = number
  description = "newbits added to vpc_cidr to size database subnets."
  default     = 8
}

variable "enable_nat_gateway" {
  type        = bool
  description = "Whether to create NAT Gateways for private subnet egress. Set false for cost saving in dev if no egress needed."
  default     = true
}

variable "single_nat_gateway" {
  type        = bool
  description = "Create only one NAT Gateway instead of one per AZ. Saves cost in dev; not HA."
  default     = false
}
