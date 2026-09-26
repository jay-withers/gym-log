# terraform

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
| ---- | ------- |
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.6 |
| <a name="requirement_azurerm"></a> [azurerm](#requirement\_azurerm) | ~> 5.0 |
| <a name="requirement_random"></a> [random](#requirement\_random) | >= 3.3.2 |

## Providers

| Name | Version |
| ---- | ------- |
| <a name="provider_azurerm"></a> [azurerm](#provider\_azurerm) | 5.6.0 |

## Modules

| Name | Source | Version |
| ---- | ------ | ------- |
| <a name="module_naming"></a> [naming](#module\_naming) | Azure/naming/azurerm | ~> 0.4 |
| <a name="module_naming_garmin_sync"></a> [naming\_garmin\_sync](#module\_naming\_garmin\_sync) | Azure/naming/azurerm | ~> 0.4 |
| <a name="module_naming_insight"></a> [naming\_insight](#module\_naming\_insight) | Azure/naming/azurerm | ~> 0.4 |

## Resources

| Name | Type |
| ---- | ---- |
| [azurerm_container_app.this](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/container_app) | resource |
| [azurerm_container_app_custom_domain.this](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/container_app_custom_domain) | resource |
| [azurerm_container_app_environment_managed_certificate.this](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/container_app_environment_managed_certificate) | resource |
| [azurerm_container_app_job.garmin_sync](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/container_app_job) | resource |
| [azurerm_container_app_job.insight](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/container_app_job) | resource |
| [azurerm_key_vault.this](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/key_vault) | resource |
| [azurerm_resource_group.this](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/resource_group) | resource |
| [azurerm_role_assignment.deployer_log_contributor](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/role_assignment) | resource |
| [azurerm_role_assignment.deployer_secrets_officer](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/role_assignment) | resource |
| [azurerm_role_assignment.identity_log_contributor](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/role_assignment) | resource |
| [azurerm_role_assignment.identity_secrets_user](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/role_assignment) | resource |
| [azurerm_storage_account.log](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/storage_account) | resource |
| [azurerm_storage_container.log](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/storage_container) | resource |
| [azurerm_user_assigned_identity.this](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/user_assigned_identity) | resource |
| [azurerm_application_insights.platform](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/data-sources/application_insights) | data source |
| [azurerm_client_config.current](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/data-sources/client_config) | data source |
| [azurerm_container_app_environment.platform](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/data-sources/container_app_environment) | data source |

## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| <a name="input_custom_domain_name"></a> [custom\_domain\_name](#input\_custom\_domain\_name) | Hostname to bind to the app with a free Azure-managed certificate, e.g. `health.jaywithers.uk`. Empty creates neither the certificate nor the binding; the default `*.azurecontainerapps.io` URL always works either way. The CNAME (to that default FQDN, from `terraform output app_url`) and the `asuid.<label>` TXT record (holding `custom_domain_verification_id`, from that output) must already resolve before apply, because Azure validates both during issuance. | `string` | `""` | no |
| <a name="input_environment"></a> [environment](#input\_environment) | Deployment environment. Drives resource naming, and selects which shared platform environment this project deploys onto. | `string` | n/a | yes |
| <a name="input_image_registry"></a> [image\_registry](#input\_image\_registry) | Registry and repository prefix the image is pulled from. A public package on ghcr.io deliberately: a private one would need a `registry` block and a Key Vault-backed pull secret on the app, and there is no Azure Container Registry because ACR Basic is a flat monthly charge with no consumption tier. | `string` | `"ghcr.io/jay-withers/gym-log"` | no |
| <a name="input_image_tag"></a> [image\_tag](#input\_image\_tag) | Image tag seeding the app's **first** revision only. Every deploy after that is `make deploy IMAGE_TAG=vX.Y.Z`, because the container's image and env sit under `ignore_changes` — so a plan against an existing deployment reports no change here even when the running image has moved on. Don't read a stale-looking default as the deployed version. | `string` | `"v0.0.1"` | no |
| <a name="input_key_vault_administrator_object_ids"></a> [key\_vault\_administrator\_object\_ids](#input\_key\_vault\_administrator\_object\_ids) | Extra Entra object IDs granted Key Vault Secrets Officer and Storage Blob Data Contributor. Whoever runs `terraform apply` is always included, so this is only for a second person or a second machine. | `list(string)` | `[]` | no |
| <a name="input_platform_app_insights_name"></a> [platform\_app\_insights\_name](#input\_platform\_app\_insights\_name) | Name of the shared Application Insights instance the app reports telemetry to. | `string` | `"appi-platform-dev"` | no |
| <a name="input_platform_environment_name"></a> [platform\_environment\_name](#input\_platform\_environment\_name) | Name of the shared Container Apps environment this app runs on. | `string` | `"cae-platform-dev"` | no |
| <a name="input_platform_resource_group_name"></a> [platform\_resource\_group\_name](#input\_platform\_resource\_group\_name) | Resource group holding the shared Container Apps environment. | `string` | `"rg-platform-dev"` | no |
| <a name="input_project_name"></a> [project\_name](#input\_project\_name) | Short name for this project, used by the naming module for every resource. | `string` | `"gymlog"` | no |
| <a name="input_tags"></a> [tags](#input\_tags) | Tags merged over the defaults in locals.tf. | `map(string)` | `{}` | no |

## Outputs

| Name | Description |
| ---- | ----------- |
| <a name="output_app_url"></a> [app\_url](#output\_app\_url) | The application's stable HTTPS URL. Bookmark this one; it survives deploys. |
| <a name="output_container_app_job_garmin_name"></a> [container\_app\_job\_garmin\_name](#output\_container\_app\_job\_garmin\_name) | Name of the daily Garmin sync job, which `make deploy` passes to `az containerapp job update`. |
| <a name="output_container_app_job_name"></a> [container\_app\_job\_name](#output\_container\_app\_job\_name) | Name of the weekly insight job, which `make deploy` passes to `az containerapp job update`. |
| <a name="output_container_app_name"></a> [container\_app\_name](#output\_container\_app\_name) | Name of the container app, which `make deploy` passes to `az containerapp update`. |
| <a name="output_custom_domain_url"></a> [custom\_domain\_url](#output\_custom\_domain\_url) | The bound custom domain, if var.custom\_domain\_name is set. Empty otherwise. |
| <a name="output_custom_domain_verification_id"></a> [custom\_domain\_verification\_id](#output\_custom\_domain\_verification\_id) | Domain verification ID, published as the `asuid.<label>` TXT record before apply. |
| <a name="output_identity_client_id"></a> [identity\_client\_id](#output\_identity\_client\_id) | Client ID of the workload identity, which the container receives as `AZURE_CLIENT_ID` and uses to reach Key Vault and the log. |
| <a name="output_key_vault_name"></a> [key\_vault\_name](#output\_key\_vault\_name) | Key Vault name, for populating APP-PASSCODE with `az keyvault secret set`. |
| <a name="output_resource_group_name"></a> [resource\_group\_name](#output\_resource\_group\_name) | This project's resource group. |
| <a name="output_state_container_url"></a> [state\_container\_url](#output\_state\_container\_url) | Blob container holding the training log, for `make deploy` and for `gymlog import` run locally. |
<!-- END_TF_DOCS -->
