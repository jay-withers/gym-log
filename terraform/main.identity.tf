# One identity for this project, used by the app to read its own Key Vault and
# the blob holding the training log.
#
# Per-project rather than one shared across the platform: identities are free,
# and this one can read the passcode guarding a publicly reachable app and
# read-write the training log. A platform-wide identity would make both
# reachable from any future tenant's image.
resource "azurerm_user_assigned_identity" "this" {
  name                = module.naming.user_assigned_identity.name
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  tags                = local.tags
}
