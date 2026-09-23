# The weekly AI insight. A second workload alongside the app, on the same
# platform environment and reusing the same image — only `args` differs.
#
# **Why a job and not an in-process timer.** The app's `min_replicas = 0`
# (see main.container-apps.tf): nothing is running most of the week, so
# nothing could fire a scheduled task inside the app process itself. A
# Container App Job is the platform's answer to "run this on a schedule
# regardless of whether anything else is up".
#
# A second naming module instance, unlike the app: a job's name must carry
# its workload to match the platform's job-failure alert convention
# (caj-<project>-<env>-<workload>, per repo-agent's naming_scan). That alert
# does *not* cover the app (see main.container-apps.tf's comment on why a
# crash-looping app raises nothing) — but it does cover this, because a job
# is exactly what it watches.
module "naming_insight" {
  # checkov:skip=CKV_TF_1: Terraform Registry module pinned by semver, as above.
  source  = "Azure/naming/azurerm"
  version = "~> 0.4"
  suffix  = [var.project_name, var.environment, "insight"]
}

resource "azurerm_container_app_job" "insight" {
  name                         = module.naming_insight.container_app_job.name
  resource_group_name          = azurerm_resource_group.this.name
  container_app_environment_id = data.azurerm_container_app_environment.platform.id

  # Unlike the app, which rejects this argument: a job is not attached to a
  # revision the environment can place, so it must state its own region. See
  # CLAUDE.md's repo-agent-vs-here table for the exact distinction.
  location = data.azurerm_container_app_environment.platform.location

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.this.id]
  }

  # One run a week is done well inside a couple of minutes — a prompt built
  # from a week of sessions and one DeepSeek call. Generous rather than tight,
  # since a job that times out retries at the caller's expense (another
  # DeepSeek call), not for free.
  replica_timeout_in_seconds = 300
  replica_retry_limit        = 1

  schedule_trigger_config {
    # Sunday 20:00 UTC — the week's training is done by then, and the summary
    # is there to read before the next week's first session.
    cron_expression          = "0 20 * * SUN"
    parallelism              = 1
    replica_completion_count = 1
  }

  template {
    container {
      name   = "insight"
      image  = local.image
      cpu    = local.container_cpu
      memory = local.container_memory

      # Same image, different subcommand — see the app's own comment on why
      # `command` is never set.
      args = ["insight"]

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

  # Mirrors the app: `make deploy` rolls the app's image forward, and this job
  # should track the same released version rather than the one from its own
  # last `terraform apply`.
  lifecycle {
    ignore_changes = [
      template[0].container[0].image,
      template[0].container[0].env,
    ]
  }
}
