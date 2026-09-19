config {
  call_module_type = "local"
}

plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

plugin "azurerm" {
  enabled = true
  version = "0.32.0"
  source  = "github.com/terraform-linters/tflint-ruleset-azurerm"
}

# This rule wants `lifecycle { prevent_destroy = true }` on stateful resources.
# That's the opposite of what this stack wants: it's a cheap experiment that
# should be destroyable and recreatable on demand, and prevent_destroy would make
# `terraform destroy` fail outright. Excluding the specific types rather than
# disabling the rule keeps the guard armed for anything added later (a storage
# account holding real data, say).
rule "azurerm_resources_missing_prevent_destroy" {
  enabled = true
  exclude = [
    "azurerm_key_vault",
    "azurerm_postgresql_flexible_server",
    "azurerm_postgresql_flexible_server_database",
  ]
}
