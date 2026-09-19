# Supplies object_id for the Key Vault Secrets Officer assignment.
data "azurerm_client_config" "current" {}

# The shared platform, resolved by name rather than by reading its Terraform
# state. No shared state credentials, and the coupling stays a convention.
#
# The consequence is that a plan against an environment the platform has not
# been applied to fails at plan time — which is why this repository plans `dev`
# alone rather than a dev/stg/prd matrix. Only dev exists.
data "azurerm_container_app_environment" "platform" {
  name                = var.platform_environment_name
  resource_group_name = var.platform_resource_group_name
}

# The connection string the app needs. Read from the shared resource rather
# than passed in as a variable, so it cannot drift from what the platform
# actually deployed.
data "azurerm_application_insights" "platform" {
  name                = var.platform_app_insights_name
  resource_group_name = var.platform_resource_group_name
}
