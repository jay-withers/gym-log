# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository.

`README.md` covers *why* and how to run it. This file covers the traps.

## What this repo does

A training log for two full-body sessions a week, replacing `Gym_3.xlsx`. One
container app on the **shared** Azure Container Apps environment, one Key Vault,
one managed identity, one storage account. It is the second tenant of
`jay-withers/azure-container-apps`, after `repo-agent`.

## The tenant split — do not undo this

The Container Apps environment is **not** here. It lives in
`jay-withers/azure-container-apps` and is resolved **by name** through a data
source, never `terraform_remote_state`:

```hcl
data "azurerm_container_app_environment" "platform" {
  name                = var.platform_environment_name # cae-platform-dev
  resource_group_name = var.platform_resource_group_name
}
```

Consequences that bite:

- **A container app may live in a different resource group from its environment,
  but not a different region.** The resource group takes its `location` from the
  environment data source rather than a variable, so this cannot drift.
- **A data source against a not-yet-applied dependency fails at *plan* time.**
  Only `dev` is ever applied on the platform, so this repo plans `dev` alone
  rather than a dev/stg/prd matrix. Add an environment here when the platform
  gains one.

## This is the first `azurerm_container_app` on the platform

`repo-agent` is a job; market-agent's apps live in an environment of their own.
Differences from repo-agent worth knowing before copying anything between them:

| | repo-agent (job) | here (app) |
| --- | --- | --- |
| `location` on the workload | required | **rejected** — apps inherit the environment's |
| Triggering | `schedule_trigger_config` | `ingress` + `min_replicas`/`max_replicas` |
| Dockerfile | no `EXPOSE`/`HEALTHCHECK` | both, because this serves something |
| `make deploy` | `az containerapp job update` | `az containerapp update` |
| Naming module | two instances | one — nothing parses the app's name |

**The platform's job-failure alert does not cover this app.** It splits on
`JobName_s` and watches jobs only, so a crash-looping container app raises
nothing. If that matters later it is a tenant-side log alert here, not a change
in the platform repo.

## The `command` decision — do not undo this

**The container sets no `command`.** The Dockerfile's `ENTRYPOINT` names the
console script; `args = ["serve"]` picks the subcommand.

market-agent took a full outage from the opposite arrangement in September 2026:
`command = ["marketagent"]` under `ignore_changes` went stale against a renamed
script and every workload crash-looped on `executable file not found`. Two
sources of truth for one string, only one of them versioned with the code that
defines it.

## The copied-modules tripwire has fired

`settings.py`, `telemetry.py` and `cli.py` are copied from `repo-agent`, which
copied them from `market-agent`. Each carries a header saying so.

repo-agent's rule is: *"a third consumer appears, **or** the same bug gets fixed
twice"* is the tripwire for extracting a shared library. **This is the third
consumer, so it has fired.** The decision taken was to copy once more and extract
when a bug is next fixed twice, rather than to block this project on building and
publishing a library. Re-copy rather than diverge; fix bugs in all three.

## Storage: the inversion from repo-agent

`store.py` is adapted from repo-agent's `state.py`, and **the error handling is
deliberately the opposite**.

repo-agent's state is commentary — it decides which findings are "new" — so every
failure degrades silently to an empty document and the run continues. Here the
document *is* the product. Returning an empty log on an unreadable blob would
present a year of training as a blank slate and then overwrite it with the next
session. So reads raise, writes raise, and the only tolerated absence is a blob
that has never existed.

For the same reason the storage account carries `prevent_destroy = true`, which
repo-agent's deliberately does not. `terraform destroy` fails until that block is
removed by hand. That friction is the point: it is the one resource here whose
contents cannot be reconstructed.

## Terraform conventions

- **File layout is enforced, not conventional.** `locals`/`variable`/`output`/
  `data` blocks live in a matching `locals.tf`/`variables.tf`/`outputs.tf`/
  `data.tf` or a topic-scoped variant, via `scripts/check-tf-standards.sh`. That
  script is **shared verbatim** with `market-agent`, `repo-agent`,
  `terraform-root-aks`, `azure-landingzone` and `github-repos` — a change here
  should be re-copied there rather than allowed to diverge.
- Variables split by whether they must be supplied: `variables.required.tf` (only
  `environment`) and `variables.optional.tf` (everything with a default).
- `.name`, not `.name_unique`. A random suffix would make the platform's
  by-name convention unusable.
- **The lock file must carry hashes for every platform that runs Terraform:**
  `terraform providers lock -platform=linux_amd64 -platform=linux_arm64
  -platform=darwin_arm64`. arm64-only hashes fail `pre-commit / Pre-commit` in CI
  on every PR, because CI runs amd64, adds a hash during `init`, and the modified
  tracked file trips the hook.
- `terraform console` needs a real backend, unlike `validate`. To check a
  generated name without credentials, evaluate the naming module in a throwaway
  configuration with no backend block.
- **Comments and outputs earn their place.** Comment the non-obvious — a cost
  trade-off, a provider quirk, a trap — not what the code already says.

## `make deploy` is az cli, not terraform apply

`terraform apply` seeds the first revision. After that `image` and `env` sit
under `ignore_changes` and `make deploy` owns them, so a deploy needs no state
lock and no plan of unrelated drift. The trade-off: changing any *other* value in
`common_env` needs a `make deploy` to land on a running revision — `terraform
plan` reports no diff even though the value it computes has moved.

The whole `env` map is ignored rather than `IMAGE_TAG`'s entry, because indexing
into a map-driven `dynamic` block by position would silently shift if
`common_env` gained or lost a key.

## Testing

`pytest`, flat `tests/`, one file per module. `filterwarnings = ["error"]`, with
one ignore for a deprecation raised inside starlette's own `TestClient` import —
market-agent carries the identical ignore.

Two things the fixtures exist to guarantee:

- `conftest.py` clears `KEY_VAULT_URI` and `STATE_CONTAINER_URL`. Either one set
  would let a missing value fall through to a real network call, and the suite
  would pass or fail depending on whose machine it ran on.
- `TestClient` points at `https://testserver`. The session cookie is `Secure`, and
  an http client accepts it and then never sends it back — every gated request
  would 303 and the cause would look like a bug in the gate.

`pytest-cov` is deliberately not declared; the shared CI workflow injects it.
`extras: dev` matters in CI: pytest is an extra, not a dependency group, and
`uv run` installs groups but not extras.

## Docker

- **Build `--platform linux/amd64`.** Container Apps is amd64-only and the dev
  host is arm64; a native build crash-loops with an exec format error and no
  other clue. The Makefile does it rather than leaving it to memory.
- **QEMU emulation swallows Python logging.** If an emulated container fails
  silently, rebuild natively before believing the code is at fault.
- One `FROM ... AS base` that both stages derive from. Naming the Python version
  twice let a Renovate "non-major" bump move the runtime to 3.14 and leave the
  builder on 3.12, producing an image with no site-packages on `sys.path`.
- `uv sync --no-editable`, or the runtime stage copies a venv whose `.pth` points
  at a build-stage path that does not exist.
- The `org.opencontainers.image.source` label is what links the package to this
  repository, and that link is what gives a workflow's `GITHUB_TOKEN` write
  access to it.
- **New GHCR packages default to private** regardless of repository visibility,
  and there is no pull secret. Flip it once after the first push.

## Commit messages

Conventional Commits, enforced by commitlint at commit-msg time. The
`no-commit-to-branch` hook blocks direct commits to `main`.

## Repo settings

Branch protection and repository settings are **not** configured here. This repo
is managed by [jay-withers/github-repos](https://github.com/jay-withers/github-repos).
Applying a required check that nothing reports leaves every PR *pending* rather
than failing it — check contexts against `gh pr checks` before applying.
