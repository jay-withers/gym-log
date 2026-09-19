output "resource_group_name" {
  description = "This project's resource group."
  value       = azurerm_resource_group.this.name
}

# The whole point of the deployment: the URL opened on a phone. Taken from the
# ingress block rather than `latest_revision_fqdn`, which changes with every
# revision and so is the wrong thing to bookmark.
output "app_url" {
  description = "The application's stable HTTPS URL. Bookmark this one; it survives deploys."
  value       = "https://${azurerm_container_app.this.ingress[0].fqdn}"
}

output "container_app_name" {
  description = "Name of the container app, which `make deploy` passes to `az containerapp update`."
  value       = azurerm_container_app.this.name
}

output "key_vault_name" {
  description = "Key Vault name, for populating APP-PASSCODE with `az keyvault secret set`."
  value       = azurerm_key_vault.this.name
}

output "identity_client_id" {
  description = "Client ID of the workload identity, which the container receives as `AZURE_CLIENT_ID` and uses to reach Key Vault and the log."
  value       = azurerm_user_assigned_identity.this.client_id
}

# What `make deploy` pushes onto the running revision, because `common_env` sits
# under `ignore_changes` and Terraform will therefore never update it itself.
output "state_container_url" {
  description = "Blob container holding the training log, for `make deploy` and for `gymlog import` run locally."
  value       = "${azurerm_storage_account.log.primary_blob_endpoint}${azurerm_storage_container.log.name}"
}

# --- the custom domain --------------------------------------------------------
#
# Both are empty when var.custom_domain_name is unset, so `make url` falls back
# to app_url and `make dns` prints nothing to do.

output "custom_domain_url" {
  description = "The bound custom domain, if var.custom_domain_name is set. Empty otherwise."
  value       = var.custom_domain_name != "" ? "https://${var.custom_domain_name}" : ""
}

# The value of the asuid.<label> TXT record. Azure checks it during binding to
# prove the domain is ours, and it is stable for the life of the app.
output "custom_domain_verification_id" {
  description = "Domain verification ID, published as the `asuid.<label>` TXT record before apply."
  value       = azurerm_container_app.this.custom_domain_verification_id
  sensitive   = true
}
