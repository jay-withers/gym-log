# The nightly off-Azure backup (`gymlog backup`, src/gymlog/backup.py): an
# age-encrypted copy of the log in the cloudflare repo's R2 bucket, downloaded
# again and decrypted with the drill key on every run to prove it restores.
#
# A job inside this app rather than a GitHub workflow because this identity
# already reads the log: a workflow would need a new grant on the training
# history for the pipeline identity. Running here also puts the job under the
# platform's job-failure alert and observability's heartbeat check (both learn
# jobs by name, caj-<project>-<env>-<workload>) with nothing added there.
module "naming_backup" {
  # checkov:skip=CKV_TF_1: Terraform Registry module pinned by semver, as above.
  source  = "Azure/naming/azurerm"
  version = "~> 0.4"
  suffix  = [var.project_name, var.environment, "backup"]
}

resource "azurerm_container_app_job" "backup" {
  name                         = module.naming_backup.container_app_job.name
  resource_group_name          = data.azurerm_resource_group.this.name
  container_app_environment_id = data.azurerm_container_app_environment.platform.id

  # Unlike the app, which rejects this argument: see main.container-apps-job.tf.
  location = data.azurerm_container_app_environment.platform.location

  # See main.container-apps.tf's identical argument on the app resource.
  workload_profile_name = "Consumption"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.this.id]
  }

  # A 100 KB document, two uploads and two downloads. Five minutes is mostly
  # the cold start; the retry covers a transient R2 or Key Vault error, and a
  # second copy under the same dated key is harmless.
  replica_timeout_in_seconds = 300
  replica_retry_limit        = 1

  schedule_trigger_config {
    # 02:30 UTC, after the day's sessions and before the 09:00 Garmin sync, so
    # each daily copy is the previous day's log as it ended.
    cron_expression          = "30 2 * * *"
    parallelism              = 1
    replica_completion_count = 1
  }

  template {
    container {
      name   = "backup"
      image  = local.image
      cpu    = local.container_cpu
      memory = local.container_memory

      # Same image, different subcommand. See the app's own comment on why
      # `command` is never set.
      args = ["backup"]

      dynamic "env" {
        for_each = local.backup_env
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  tags = local.tags

  # As the other jobs: `make deploy` owns the image and the env.
  lifecycle {
    ignore_changes = [
      template[0].container[0].image,
      template[0].container[0].env,
    ]
  }
}
