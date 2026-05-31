locals {
  # Single source of truth for tags — every module and resource merges this.
  common_tags = {
    Project     = var.project_name
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}
