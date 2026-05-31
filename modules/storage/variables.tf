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

variable "buckets" {
  description = "Map of logical bucket names to config. Key becomes part of the bucket name."
  type = map(object({
    versioning      = bool
    glacier_days    = optional(number, 0)   # 0 = no lifecycle transition
    expiry_days     = optional(number, 0)   # 0 = no expiry rule
    force_destroy   = optional(bool, false) # true in dev so `terraform destroy` works cleanly
  }))
}
