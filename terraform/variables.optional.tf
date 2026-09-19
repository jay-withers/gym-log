variable "project_name" {
  description = "Short name for this project, used by the naming module for every resource."
  type        = string
  default     = "gymlog"

  validation {
    # Lowercase only (container apps reject uppercase), and short enough that
    # `ca-<project>-<env>` fits the 32 characters container apps allow — the
    # naming module truncates silently rather than failing. At `gymlog` the app
    # is `ca-gymlog-dev`, 13 of 32. Changing this is also how to resolve a clash
    # on the globally unique Key Vault or storage account name.
    condition     = can(regex("^[a-z][a-z0-9]{1,14}$", var.project_name))
    error_message = "project_name must be 2-15 lowercase alphanumerics starting with a letter."
  }
}

variable "tags" {
  description = "Tags merged over the defaults in locals.tf."
  type        = map(string)
  default     = {}
}

variable "key_vault_administrator_object_ids" {
  description = "Extra Entra object IDs granted Key Vault Secrets Officer and Storage Blob Data Contributor. Whoever runs `terraform apply` is always included, so this is only for a second person or a second machine."
  type        = list(string)
  default     = []
}

# --- the shared platform ------------------------------------------------------
#
# Resolved by name rather than from the platform's Terraform state. These
# defaults match jay-withers/azure-container-apps at its own defaults; run
# `make outputs` there to confirm.

variable "platform_resource_group_name" {
  description = "Resource group holding the shared Container Apps environment."
  type        = string
  default     = "rg-platform-dev"
}

variable "platform_environment_name" {
  description = "Name of the shared Container Apps environment this app runs on."
  type        = string
  default     = "cae-platform-dev"
}

variable "platform_app_insights_name" {
  description = "Name of the shared Application Insights instance the app reports telemetry to."
  type        = string
  default     = "appi-platform-dev"
}

# --- the image ----------------------------------------------------------------

variable "image_registry" {
  description = "Registry and repository prefix the image is pulled from. A public package on ghcr.io deliberately: a private one would need a `registry` block and a Key Vault-backed pull secret on the app, and there is no Azure Container Registry because ACR Basic is a flat monthly charge with no consumption tier."
  type        = string
  default     = "ghcr.io/jay-withers/gym-log"
}

variable "image_tag" {
  description = "Image tag seeding the app's **first** revision only. Every deploy after that is `make deploy IMAGE_TAG=vX.Y.Z`, because the container's image and env sit under `ignore_changes` — so a plan against an existing deployment reports no change here even when the running image has moved on. Don't read a stale-looking default as the deployed version."
  type        = string
  # Must name a tag that actually exists in the registry. cd-tag's first release
  # on a repo with no prior tags is v0.0.1, not v0.1.0 — and a tag that does not
  # exist fails at revision start-up on the image pull, long after both plan and
  # apply have reported success.
  default = "v0.0.1"

  validation {
    # A moving tag deploys nothing: Container Apps creates a revision only when
    # the template changes, so re-pushing `latest` reports success and changes
    # nothing at all.
    condition     = !contains(["latest", "main", "unset"], var.image_tag)
    error_message = "image_tag must be an immutable tag, not latest/main/unset."
  }
}

# --- the custom domain --------------------------------------------------------

variable "custom_domain_name" {
  description = "Hostname to bind to the app with a free Azure-managed certificate, e.g. `gymlog.jaywithers.uk`. Empty creates neither the certificate nor the binding; the default `*.azurecontainerapps.io` URL always works either way. The CNAME (to that default FQDN) and the `asuid.<label>` TXT record (holding `custom_domain_verification_id`) must already resolve before apply, because Azure validates both during issuance — `make dns` prints them."
  type        = string
  default     = ""

  validation {
    # An apex domain cannot be a CNAME, and CNAME is the only validation method
    # these resources use. Caught here rather than as an opaque DNS failure
    # midway through certificate issuance.
    condition     = var.custom_domain_name == "" || length(split(".", var.custom_domain_name)) > 2
    error_message = "custom_domain_name must be a subdomain, not an apex domain: validation is by CNAME, which an apex record cannot hold."
  }
}
