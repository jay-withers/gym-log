variable "environment" {
  description = "Deployment environment. Drives resource naming, and selects which shared platform environment this project deploys onto."
  type        = string

  validation {
    condition     = contains(["dev", "stg", "prd"], var.environment)
    error_message = "environment must be one of: dev, stg, prd."
  }
}

variable "key_vault_administrator_object_ids" {
  description = "Entra user object IDs (not groups or service principals: the grants set `principal_type = \"User\"`) granted Key Vault Secrets Officer and Storage Blob Data Contributor. Listed explicitly rather than taken from whoever runs `terraform apply`, because cd-terraform applies as the pipeline identity. Not committed: CI reads it from the TF_VAR_KEY_VAULT_ADMINISTRATOR_OBJECT_IDS variable github-repos sets on the `dev` environment; a local plan needs TF_VAR_key_vault_administrator_object_ids exported."
  type        = list(string)
}
