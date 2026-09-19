variable "environment" {
  description = "Deployment environment. Drives resource naming, and selects which shared platform environment this project deploys onto."
  type        = string

  validation {
    condition     = contains(["dev", "stg", "prd"], var.environment)
    error_message = "environment must be one of: dev, stg, prd."
  }
}
