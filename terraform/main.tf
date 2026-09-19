# This project's own infrastructure. The Container Apps environment it runs on
# is **not** here — it is shared, lives in jay-withers/azure-container-apps, and
# is resolved by name in data.tf.
#
# Everything this project owns is in its own resource group with its own Key
# Vault, identity and storage, so the log and the passcode are readable by
# nothing else on the platform.
module "naming" {
  # checkov:skip=CKV_TF_1: Terraform Registry module pinned by semver
  # (version below), not a git source — there's no commit hash to pin.
  source  = "Azure/naming/azurerm"
  version = "~> 0.4"
  suffix  = [var.project_name, var.environment]
}

# Only one naming instance, unlike repo-agent's second `naming_scan`. That one
# exists because a job's name must carry its workload to match the platform's
# job-failure alert convention (caj-<project>-<env>-<workload>). There is one
# container app here and nothing parses its name, so `ca-gymlog-dev` (13 of the
# 32 characters container apps allow) is the whole of it.

resource "azurerm_resource_group" "this" {
  name = module.naming.resource_group.name
  # Must match the shared environment's region: a container app may sit in a
  # different resource group from its environment, but not a different region.
  location = data.azurerm_container_app_environment.platform.location
  tags     = local.tags
}
