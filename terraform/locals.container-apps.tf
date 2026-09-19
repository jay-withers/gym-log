locals {
  # Assembled by hand: `azurerm_storage_container` exports no Resource Manager
  # id, and its `id` is the data-plane URL, which a role assignment rejects.
  log_container_scope = "${azurerm_storage_account.log.id}/blobServices/default/containers/${azurerm_storage_container.log.name}"

  image = "${var.image_registry}/gymlog:${var.image_tag}"

  # uvicorn binds this, the Dockerfile EXPOSEs it, and both probes below check it.
  target_port = 8000

  # The smallest combination Container Apps accepts; memory must be 2 GiB per
  # vCPU. This renders a handful of Jinja templates against a 100 KB document —
  # it is bounded by the cold start, not by CPU.
  container_cpu    = 0.25
  container_memory = "0.5Gi"

  # Everything the container needs that is not a secret. The passcode is
  # resolved at runtime from Key Vault by settings.py, not injected here — an
  # env var is visible in `az containerapp show` output and in state.
  common_env = {
    AZURE_CLIENT_ID                       = azurerm_user_assigned_identity.this.client_id
    KEY_VAULT_URI                         = azurerm_key_vault.this.vault_uri
    APPLICATIONINSIGHTS_CONNECTION_STRING = data.azurerm_application_insights.platform.connection_string
    ENVIRONMENT                           = var.environment
    # Recorded so a trace names the build that produced it.
    IMAGE_TAG = var.image_tag
    # Where the log is read and written. Not a secret — it is a URL, and
    # reaching it still needs the managed identity's RBAC grant.
    STATE_CONTAINER_URL = "${azurerm_storage_account.log.primary_blob_endpoint}${azurerm_storage_container.log.name}"
  }
}
