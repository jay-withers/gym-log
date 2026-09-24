# The daily Garmin sync. A third workload alongside the app and the insight
# job, on the same platform environment and reusing the same image — only
# `args` differs. See main.container-apps-job.tf's comment for why this is a
# job rather than an in-process timer: the app's `min_replicas = 0` means
# nothing runs most of the day, so nothing could fire a schedule itself.
#
# Daily rather than weekly: Garmin data (steps, sleep, resting heart rate) is
# generated every day, and the sync's own retention window is a rolling 30
# days — this just needs to run often enough that a single missed day is
# invisible against that window, not to catch every day the instant it's
# available.
#
# A third naming module instance, same reasoning as `naming_insight`: a job's
# name carries its workload so the platform's job-failure alert
# (caj-<project>-<env>-<workload>) watches it the same way it already watches
# the insight job.
module "naming_garmin_sync" {
  # checkov:skip=CKV_TF_1: Terraform Registry module pinned by semver, as above.
  source  = "Azure/naming/azurerm"
  version = "~> 0.4"
  suffix  = [var.project_name, var.environment, "garmin"]
}

resource "azurerm_container_app_job" "garmin_sync" {
  name                         = module.naming_garmin_sync.container_app_job.name
  resource_group_name          = azurerm_resource_group.this.name
  container_app_environment_id = data.azurerm_container_app_environment.platform.id

  # Unlike the app, which rejects this argument — see main.container-apps-job.tf.
  location = data.azurerm_container_app_environment.platform.location

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.this.id]
  }

  # A fixed 30-day window of activities and daily summaries, several Garmin
  # API calls each — more than the insight job's single LLM call, so more
  # generous headroom. A timeout retries at Garmin's expense (another round of
  # calls against an unofficial API), not for free, so this is generous rather
  # than tight.
  replica_timeout_in_seconds = 600
  replica_retry_limit        = 1

  schedule_trigger_config {
    # 05:00 UTC — early enough that the prior day's Garmin data (sleep
    # especially, which finalizes on waking) has settled by the time this runs.
    cron_expression          = "0 5 * * *"
    parallelism              = 1
    replica_completion_count = 1
  }

  template {
    container {
      name   = "garmin-sync"
      image  = local.image
      cpu    = local.container_cpu
      memory = local.container_memory

      # Same image, different subcommand — see the app's own comment on why
      # `command` is never set.
      args = ["garmin-sync"]

      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  tags = local.tags

  # Mirrors the app and the insight job: `make deploy` rolls the image forward,
  # and this job should track the same released version rather than the one
  # from its own last `terraform apply`.
  lifecycle {
    ignore_changes = [
      template[0].container[0].image,
      template[0].container[0].env,
    ]
  }
}
