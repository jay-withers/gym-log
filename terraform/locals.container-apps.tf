locals {
  # Assembled by hand: `azurerm_storage_container` exports no Resource Manager
  # id, and its `id` is the data-plane URL, which a role assignment rejects.
  log_container_scope = "${azurerm_storage_account.log.id}/blobServices/default/containers/${azurerm_storage_container.log.name}"

  # **The first revision runs a placeholder, not the app.** Terraform only
  # needs an image to create the app and its jobs; the first `make deploy`
  # (cd-publish, on the first release) puts the real one on, and the image is
  # under `ignore_changes`, so Terraform never touches it again. The placeholder
  # answers 200 on every path on $PORT, so the probes pass, and exits 0 at once
  # in a job. This replaced seeding from a guessed release tag, which passes
  # plan and apply and only fails when the revision pulls it.
  #
  # `latest` deliberately: dev-containers prunes all but its last ten releases
  # and never `latest`, and a moving tag's usual trap (a re-push deploys
  # nothing) can't apply to an image used only at create.
  #
  # **A job or app that is replaced (a ForceNew change) comes back on this**,
  # and a job then "succeeds" doing nothing. Run `make deploy` after any apply
  # that replaces one.
  image = "ghcr.io/jay-withers/dev-containers/placeholder:latest"

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
    # IMAGE_TAG, which a trace records, is set by `make deploy` alongside the
    # image: the placeholder Terraform creates with has no release tag.
    # Where the log is read and written. Not a secret — it is a URL, and
    # reaching it still needs the managed identity's RBAC grant.
    STATE_CONTAINER_URL = "${azurerm_storage_account.log.primary_blob_endpoint}${azurerm_storage_container.log.name}"
  }

  # The custom domain when one is bound, since that is the URL actually used.
  public_url = var.custom_domain_name != "" ? "https://${var.custom_domain_name}" : "https://${azurerm_container_app.this.ingress[0].fqdn}"

  # Only the insight job emails, so only it gets the sender and the link.
  insight_env = merge(local.common_env, {
    INSIGHT_EMAIL_FROM = var.insight_email_from
    PUBLIC_URL         = local.public_url
  })
}
