# Where the training log lives.
#
# One small JSON document, read whole on each request and written whole when a
# session is saved. That access pattern is why this is a blob rather than a
# table: there is no query to serve. At two sessions a week — seven exercises,
# three sets — it grows by roughly 2 KB a week, so a year is about 100 KB.
#
# PostgreSQL was considered and rejected. market-agent's Flexible Server bills
# ~£13/month whether or not anything uses it, and it was *forced* there by a
# subscription policy blocking Azure SQL rather than chosen — see the header of
# that repo's main.database.tf. Nothing here needs it.
resource "azurerm_storage_account" "log" {
  # **`prevent_destroy` is set, and this is a deliberate divergence from
  # repo-agent**, which leaves it off so dev can be torn down freely. Its state
  # document is commentary — a lost one costs a week of "is this finding new".
  # This one is the product: years of training history that exists nowhere else
  # and that no subsequent run reconstructs. Destroying the rest of this stack
  # should not take it with it.
  #
  # The cost is real and intended: `terraform destroy` fails until this block is
  # removed by hand. That is the right amount of friction for the one resource
  # here whose contents are irreplaceable.
  #
  # checkov:skip=CKV_AZURE_206: LRS on purpose. LRS is already eleven nines of
  #   durability within the region; the realistic risk to this document is a bad
  #   write or an accidental delete, which versioning and the retention policies
  #   below cover and which geo-redundancy would not.
  # checkov:skip=CKV_AZURE_59: public network access stays enabled, for the same
  #   reason as the Key Vault — a scale-to-zero container app on a
  #   Consumption-only shared environment has neither a VNet to peer nor a
  #   static egress IP to allow.
  # checkov:skip=CKV_AZURE_33: no queues are used, so queue logging has nothing
  #   to log.
  # checkov:skip=CKV2_AZURE_1: platform-managed keys. A CMK needs a second Key
  #   Vault key, rotation and a managed identity grant on it, to protect a list
  #   of how much someone lifted.
  # checkov:skip=CKV2_AZURE_33: no private endpoint, as above.
  # checkov:skip=CKV2_AZURE_47: public access as above.
  name                = module.naming.storage_account.name
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location

  account_tier             = "Standard"
  account_kind             = "StorageV2"
  account_replication_type = "LRS"

  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = true
  allow_nested_items_to_be_public = false
  public_network_access           = "Enabled"

  # **Entra ID only.** Turning off shared keys means no connection string and no
  # SAS exists to leak, and the managed identity below is the only way in. It
  # also means `az storage blob` needs `--auth-mode login`.
  shared_access_key_enabled = false

  blob_properties {
    # Every save becomes a version, which is the undo this application does not
    # otherwise have: a session logged against the wrong day, or a weight typed
    # with a misplaced decimal, is recoverable rather than argued with. It costs
    # a few hundred kilobytes a year.
    versioning_enabled = true

    delete_retention_policy {
      days = 30
    }
    container_delete_retention_policy {
      days = 30
    }
  }

  lifecycle {
    prevent_destroy = true
  }

  tags = local.tags
}

# tflint-ignore: azurerm_resources_missing_prevent_destroy
resource "azurerm_storage_container" "log" {
  # The container itself carries no `prevent_destroy`: the account above already
  # blocks the destroy, and the container delete retention policy covers the
  # rest. Duplicating it here would mean two blocks to remove rather than one on
  # the day this is genuinely being torn down.
  #
  # checkov:skip=CKV2_AZURE_21: no blob read logging. It would land in the shared
  #   Log Analytics workspace, whose `daily_quota_gb = 0.15` is split across every
  #   tenant on the platform — spent on recording that a page load read one file.
  name                  = "state"
  storage_account_id    = azurerm_storage_account.log.id
  container_access_type = "private"
}

# The container app reads and writes the log through this.
#
# Scoped to the container rather than the account: the identity has no reason to
# reach any other container, and this one is the only thing in here.
#
# The scope is assembled by hand because `azurerm_storage_container` exports no
# Resource Manager id — its `id` is the data-plane URL, which a role assignment
# will not accept.
resource "azurerm_role_assignment" "identity_log_contributor" {
  scope                = local.log_container_scope
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.this.principal_id
  # Stated explicitly: without it the provider looks the principal up in the
  # directory, which fails intermittently on an identity created moments ago.
  principal_type = "ServicePrincipal"
}

# Whoever applies can read and edit the log by hand — which is what
# `gymlog import` and `gymlog show` do, running locally against the real blob.
resource "azurerm_role_assignment" "deployer_log_contributor" {
  for_each = toset(concat(
    [data.azurerm_client_config.current.object_id],
    var.key_vault_administrator_object_ids,
  ))

  scope                = local.log_container_scope
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = each.value
}
