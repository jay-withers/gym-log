# The web application.
#
# Runs on the shared platform environment (see data.tf) but lives in this
# project's own resource group. That is allowed across resource groups but not
# across regions, which is why the resource group takes its location from the
# environment rather than from a variable of its own.
#
# **This is the first `azurerm_container_app` on the platform.** repo-agent is a
# job and market-agent's apps live in an environment of their own, so the
# differences from repo-agent are called out inline rather than left to be
# rediscovered.
resource "azurerm_container_app" "this" {
  name                         = module.naming.container_app.name
  container_app_environment_id = data.azurerm_container_app_environment.platform.id
  resource_group_name          = azurerm_resource_group.this.name

  # No `location` argument, unlike repo-agent's job: a container app inherits
  # its environment's region and the provider rejects the argument outright.
  # Jobs are the exception that must state it.

  # Single, not Multiple. There is one user and one URL; weighted traffic across
  # revisions would mean a session logged against whichever revision answered.
  revision_mode = "Single"

  # The shared environment has no other workload profile (it is
  # Consumption-only, the same reason the storage account's checkov skips give
  # for no VNet/private endpoint). Stated explicitly rather than left to the
  # provider default: the API already reports this back, and leaving it unset
  # in config made every plan propose clearing it.
  workload_profile_name = "Consumption"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.this.id]
  }

  ingress {
    # Reachable from the internet, which is the entire point — it is opened on a
    # phone, on mobile data, in a gym. The passcode gate in api/deps.py is what
    # stands in front of it, and it defaults on.
    external_enabled = true
    target_port      = local.target_port
    transport        = "auto"

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    # **Scale to zero, and this is the cost decision that matters.** A standing
    # replica at 0.25 vCPU / 0.5 GiB runs past the Container Apps monthly free
    # grant to roughly £11/month — on a platform whose entire design is that
    # nothing bills while idle, to avoid a few seconds' wait twice a week.
    #
    # The cold start is paid once per workout rather than once per set: the
    # replica stays warm for the hour it is being used and only scales back down
    # long after. See the README for the measured figure.
    min_replicas = 0
    # One. The log is a single document read-modify-written on each save; a
    # second replica would make the ETag precondition in store.py load-bearing
    # rather than belt-and-braces.
    max_replicas = 1

    container {
      name   = "gymlog"
      image  = local.image
      cpu    = local.container_cpu
      memory = local.container_memory

      # **No `command` argument, deliberately.** The image's ENTRYPOINT already
      # names the console script, and repeating it here would be a second source
      # of truth for one string — one that Terraform cannot see change.
      #
      # market-agent took a full outage from exactly that in September 2026: its
      # `command = ["marketagent"]` sat under `ignore_changes`, went stale when
      # the console script was renamed, and every workload crash-looped on
      # `exec: "investagent": executable file not found`. The Terraform there had
      # already been updated; the ignore meant the apply never pushed it, and the
      # az-cli deploy path does not touch `command` at all.
      #
      # `args` picks the subcommand and is deliberately **not** ignored below: it
      # changes rarely, and a subcommand that does not exist fails loudly with a
      # usage error rather than silently running the wrong workload.
      args = ["serve"]

      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.key
          value = env.value
        }
      }

      # **The two probes are different on purpose.** Liveness decides whether to
      # restart the container, so it must not depend on storage — a blob blip
      # would otherwise restart every replica and turn a brief outage into a
      # crash loop. /healthz answers from memory; /readyz is the one that reads.
      liveness_probe {
        transport               = "HTTP"
        port                    = local.target_port
        path                    = "/healthz"
        initial_delay           = 5
        interval_seconds        = 30
        timeout                 = 5
        failure_count_threshold = 3
      }

      readiness_probe {
        transport               = "HTTP"
        port                    = local.target_port
        path                    = "/readyz"
        interval_seconds        = 10
        timeout                 = 5
        failure_count_threshold = 3
        success_count_threshold = 1
      }
    }
  }

  tags = local.tags

  # `make deploy` (az cli) owns the running image and env after the first
  # revision, so that a deploy needs no state lock, no plan of unrelated drift,
  # and no risk of a stale local tfvars rolling the image backwards.
  #
  # The whole `env` map is ignored rather than just IMAGE_TAG's entry, because
  # indexing into a map-driven `dynamic` block by position would silently shift
  # if common_env gained or lost a key. The trade-off: a change to any *other*
  # value in common_env needs a `make deploy` to land on a running revision —
  # `terraform plan` will report no diff even though the value it computes has
  # moved.
  #
  # Note `command` is absent from this list because it is absent from the
  # resource. That is the point.
  lifecycle {
    ignore_changes = [
      template[0].container[0].image,
      template[0].container[0].env,
    ]
  }
}

# --- the custom domain --------------------------------------------------------
#
# Both resources are guarded by the same count on var.custom_domain_name. A
# hostname binds to exactly one container app globally, so an unconditional
# binding would make a second environment's plan claim the domain that dev
# already holds, the moment one ever exists.
#
# **The certificate is created in the *platform's* resource group, not this
# project's.** It is a child of the environment and takes its resource group
# from the environment's id, so this is the one resource here that lands
# outside azurerm_resource_group.this — the apply identity needs write access
# there, which being subscription-scoped it has. Deleting this project does not
# delete the platform, so the certificate is cleaned up by the count going back
# to zero, not by the resource group going away.
#
# **Both DNS records must resolve before apply.** Azure validates them during
# issuance and binding, not after: the CNAME for health.jaywithers.uk pointing
# at the app's default *.azurecontainerapps.io FQDN, and the TXT record at
# asuid.health carrying custom_domain_verification_id. `make dns` prints both
# with their values filled in.
resource "azurerm_container_app_environment_managed_certificate" "this" {
  count = var.custom_domain_name != "" ? 1 : 0

  name                         = replace(var.custom_domain_name, ".", "-")
  container_app_environment_id = data.azurerm_container_app_environment.platform.id
  subject_name                 = var.custom_domain_name
  domain_control_validation    = "CNAME"

  # Azure requires the hostname to already be registered on an app or route in
  # the environment before it will issue the managed certificate.
  depends_on = [azurerm_container_app_custom_domain.this]
}

resource "azurerm_container_app_custom_domain" "this" {
  count = var.custom_domain_name != "" ? 1 : 0

  name                     = var.custom_domain_name
  container_app_id         = azurerm_container_app.this.id
  certificate_binding_type = "Disabled"

  # container_app_environment_certificate_id is deliberately unset. That field
  # accepts a bring-your-own azurerm_container_app_environment_certificate
  # only; passing a *managed* certificate's id 400s the plan, because its id
  # carries a `managedCertificates` segment the provider's parser rejects
  # (hashicorp/terraform-provider-azurerm#25788).
  #
  # **The apply cannot finish the bind, and says nothing about it.** It reports
  # certificate_binding_type = "Disabled" with no error while
  # `az containerapp hostname list` still shows BindingType Disabled: ARM's
  # bind operation needs the certificate id in the request and the provider has
  # no field to put it in (hashicorp/terraform-provider-azurerm#27362, open).
  # Until it lands, `make bind-domain` is the manual step after any apply that
  # recreates either of these two resources. A plan afterwards reports no diff,
  # because the CLI sets the same binding type this resource already declares.
  #
  # market-agent hit and confirmed all of this first; the workaround is the
  # same there.
}
