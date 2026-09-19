locals {
  default_tags = {
    environment = var.environment
    managed-by  = "terraform"
  }

  # var.tags takes precedence on conflicts.
  tags = merge(local.default_tags, var.tags)
}
