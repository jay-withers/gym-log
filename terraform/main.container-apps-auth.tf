# Container Apps' built-in authentication (EasyAuth) in front of the app: an
# Entra sign-in before any request reaches it, on top of the passcode, which
# stays as a second layer. Off until var.auth_client_id is set.
#
# The app registration lives in azure-landingzone's governance
# (main.app-registrations.tf), because no pipeline identity can write to
# Entra. There, sign-in is limited to the operator (app role assignment
# required), and the registration trusts this app's identity through a
# federated credential, so **no client secret exists**: the
# `override-use-mi-fic-assertion-client-id` secret on the app names the
# identity, not a password.
#
# azapi because azurerm has no resource for an authConfig. It is a child of the
# app, so `make deploy` (which only swaps the image and env) never touches it.
resource "azapi_resource" "auth" {
  count = var.auth_client_id != "" ? 1 : 0

  type      = "Microsoft.App/containerApps/authConfigs@2024-03-01"
  name      = "current"
  parent_id = azurerm_container_app.this.id

  body = {
    properties = {
      platform = {
        enabled = true
      }
      globalValidation = {
        unauthenticatedClientAction = "RedirectToLoginPage"
        redirectToProvider          = "azureactivedirectory"
        # The smoke test (scripts/smoke.sh) and the platform's checks call
        # these unauthenticated. Neither returns anything but a version and a
        # status.
        excludedPaths = ["/healthz", "/readyz"]
      }
      httpSettings = {
        requireHttps = true
      }
      identityProviders = {
        azureActiveDirectory = {
          enabled = true
          registration = {
            clientId                = var.auth_client_id
            clientSecretSettingName = local.auth_identity_secret_name
            openIdIssuer            = "https://login.microsoftonline.com/${data.azurerm_client_config.current.tenant_id}/v2.0"
          }
          validation = {
            allowedAudiences = [var.auth_client_id]
          }
        }
      }
    }
  }

  # The secret it names must be on the app first.
  depends_on = [azurerm_container_app.this]
}
