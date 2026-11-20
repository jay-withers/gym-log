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
  but not a different region.** The resource group is github-repos'
  (`create_resource_groups`), created in the environment's region and read
  here by data source.
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

**The platform's workload-crashed alert covers this app.** It started as
job-only, but since 2026-09-22 it matches `ContainerAppName_s` as well as
`JobName_s`, so a crash-looping container app here raises it like a failed job
does. Nothing tenant-side is needed.

## The `command` decision — do not undo this

**The container sets no `command`.** The Dockerfile's `ENTRYPOINT` names the
console script; `args = ["serve"]` picks the subcommand.

market-agent took a full outage from the opposite arrangement in September 2026:
`command = ["marketagent"]` under `ignore_changes` went stale against a renamed
script and every workload crash-looped on `executable file not found`. Two
sources of truth for one string, only one of them versioned with the code that
defines it.

## Shared code comes from appkit

The modules career, finances and gym-log used to copy from one another now
come from [jay-withers/appkit](https://github.com/jay-withers/appkit), pinned
by tag in `pyproject.toml`'s `[tool.uv.sources]` (the builder image carries
git for it). Renovate raises the bump, `uv lock` included, like any other.

- `appkit.headers`: security headers and the CSP nonce (`api/main.py`).
- `appkit.manifest`: the home-screen install manifest (`/manifest.json` in
  `api/routes.py`), which opens on `/` unless given another `start_url`.
- `appkit.passcode`: the gate. `api/deps.py` only names the cookie.
- `appkit.keyvault`: secret resolution. `settings.py` keeps `Settings` and the
  cached `secret`/`optional_secret`/`credential` wrappers the tests clear.
- `appkit.backup` and `appkit.selfcheck`: the job and the check loop.
  `backup.py` and `selfcheck.py` keep what is gym-log's (the documents, the
  secrets).

**Fix a bug in those in appkit, with its regression test there**, then bump the
pin here. appkit's loggers are not `gymlog`, so their lines reach Log Analytics
as console logs, not App Insights traces, which keeps telemetry's one-logger
export rule. `telemetry.py` and `cli.py` are
still copied from repo-agent, each with a header saying so.

## Storage: the inversion from repo-agent

`store.py` is adapted from repo-agent's `state.py`, and **the error handling is
deliberately the opposite**.

repo-agent's state is commentary — it decides which findings are "new" — so every
failure degrades silently to an empty document and the run continues. Here the
document *is* the product. Returning an empty log on an unreadable blob would
present a year of training as a blank slate and then overwrite it with the next
session. So reads raise, writes raise, and the only tolerated absence is a blob
that has never existed.

**`integrity.check` is the log's rules**, because `Log.from_json` forgives
everything: no negative reps or load, no load that isn't a finite number, no
session date that doesn't parse, one session per date and day, one entry per
exercise in it, no duplicate ids or Garmin dates. `store.update` refuses a change
that adds a problem (an old one is tolerated, so the log never goes read-only),
the selfcheck runs the rules and appkit's round trip against the live log after
every deploy, and the backup judges them after copying. Only the impossible is
a rule; the absurd (over `MAX_REPS`, over `MAX_LOAD_KG`) is refused at the form
in `api/routes.py`, so an old typo can't block a deploy. **A new rule must hold
for the live log**, or the next deploy's selfcheck fails and rolls back.
Problems name a date, a day and positions, never an exercise or a load: they
reach Log Analytics. The round trip keeps the stored `schema` stamp
(`integrity.parse`/`dump`), so bumping `SCHEMA_VERSION` alone is not a reported
loss. A reader that reshapes stored fields still is: that needs the new shape
written before a release depends on reading it.

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

`terraform apply` creates the app and jobs on dev-containers' `placeholder`
image (200 on every path on `$PORT`; exits 0 in a job), never a guessed release
tag. After that `image` and `env` sit under `ignore_changes` and `make deploy` owns them, so a deploy needs no state
lock and no plan of unrelated drift. The trade-off: changing any *other* value in
`common_env` needs a `make deploy` to land on a running revision — `terraform
plan` reports no diff even though the value it computes has moved.

The whole `env` map is ignored rather than `IMAGE_TAG`'s entry, because indexing
into a map-driven `dynamic` block by position would silently shift if
`common_env` gained or lost a key.

## The smoke test lives in jay-withers/workflows

`make smoke` fetches `scripts/smoke.sh` from
[jay-withers/workflows](https://github.com/jay-withers/workflows) at the
commit `cd-publish.yml` pins `container-apps-deploy.yml` to, so CI and a local
run use the same version and Renovate's bump of that pin moves it. There is
no copy here: change it there. `make smoke`/`make deployed-tag` keep the same
shape in every app repo, and `container-apps-deploy.yml` runs them around
every deploy and rolls back on failure.

`browser-check: true` in `cd-publish.yml` adds a Chromium load of the app's
`/` on its default FQDN (the login page, unauthenticated): a broken template
or a CSP block in the deployed app rolls the release back. It never signs in,
so the passcode never reaches CI.

`/healthz` and `/readyz` come from `appkit.health` (`api/main.py`'s `_ready`
is gym-log's readiness read). `/healthz` must keep reporting `version` from
`IMAGE_TAG`: without it, the smoke test can only infer the new revision is
serving from `latestReadyRevisionName`, a weaker check meant for rolling back
to images from before `version` existed.

Each job is also run once with `args = ["selfcheck"]` (`selfcheck.py`), through
ARM's job start API with the job's own template so the env and secret
references survive. **Keep `selfcheck` free of side effects** (no email, no
LLM, no Garmin login, no writes): it runs on every deploy. And add a secret to
`selfcheck.SECRETS` when a job starts reading one, or a release that lacks it
deploys green and fails at its first scheduled run.

## Testing

`pytest`, flat `tests/`, one file per module. `filterwarnings = ["error"]`, with
one ignore for a deprecation raised inside starlette's own `TestClient` import —
market-agent carries the identical ignore.

CI gates two more things, both in `test / Test`: `make typecheck` (mypy over
`src/` and `tests/`, config in `pyproject.toml`) and diff coverage. At least
80% of the lines a PR changes in `gymlog` must be run by a test. Total coverage
is reported, never gated, since a floor on it mostly measures how much of the
Azure SDK path is mocked. Don't meet the diff gate with a test that only
asserts a mock.

Two things the fixtures exist to guarantee:

- `conftest.py` clears `KEY_VAULT_URI` and `STATE_CONTAINER_URL`. Either one set
  would let a missing value fall through to a real network call, and the suite
  would pass or fail depending on whose machine it ran on.
- `TestClient` points at `https://testserver`. The session cookie is `Secure`, and
  an http client accepts it and then never sends it back — every gated request
  would 303 and the cause would look like a bug in the gate.

**The progression rule has Hypothesis property tests** (the `# --- properties`
block in `test_progression.py`, and the posted-rep-range one in `test_api.py`):
reps land in the range, load holds or rises by exactly one increment, a better
session never earns a worse suggestion, and following every suggestion always
reaches the next increment. Weights and increments are generated to the gram,
because the block form takes any typed number: that is how 7.6 + 0.1 showing
as 7.7 but being 7.699999… was caught, and why a suggestion is rounded to the
gram. A change to `progression.py` must keep them passing; a counter-example
Hypothesis finds becomes an `@example` (or a plain test) beside the property.
`conftest.py` loads the `dev` profile locally (100 random examples) and, under
`CI=true`, Hypothesis' own derandomized `ci` profile raised to 1000 examples,
so a required check never fails on one run's draw. Both have no deadline and
the function-scoped-fixture check off (`fake_secrets` is autouse), so **a
property test takes no fixtures**.

**Browser tests live in `tests/e2e/`** (Playwright, through
`appkit.testing`): the real app under uvicorn on a free port, against a log
`gymlog seed` writes to a temp dir, driven by Chromium. They sign in, open
every screen in `PAGES`, log a set and add a goal, each checked after a
reload. An autouse fixture fails any journey that leaves a console error, and
a CSP block is one, so **an inline script without its nonce fails here**
even where `test_headers.py` can't see it. Add a new screen to `PAGES`.

- The server is session-scoped, so it starts before the per-test
  `fake_secrets`: its `env` sets `STATE_CONTAINER_URL`, `KEY_VAULT_URI` and
  the rest to empty itself (empty, not removed, or `Settings` would read a
  developer's `.env` instead). Keep that list in step with `fake_secrets`.
- Journeys share one log. Make what a test writes unique (the goal title is),
  rather than assuming an empty slate.
- Locally they skip without Chromium (`make e2e` installs it). Under CI they
  fail instead: ci-python's `playwright: true` installs it inside
  `test / Test`, so a broken install can't pass as skipped.

`pytest-cov` is deliberately not declared; the shared CI workflow injects it.
`extras: dev` matters in CI: pytest is an extra, not a dependency group, and
`uv run` installs groups but not extras.

## Docker

- **Build `--platform linux/amd64`.** Container Apps is amd64-only and the dev
  host is arm64; a native build crash-loops with an exec format error and no
  other clue. The Makefile does it rather than leaving it to memory.
- **QEMU emulation swallows Python logging.** If an emulated container fails
  silently, rebuild natively before believing the code is at fault.
- Both stages come from `jay-withers/dev-containers`: `python-app-builder` for
  the builder, `python-app` for the runtime, pinned at the **same version**
  (Renovate raises them together). The builder is built FROM the runtime, so
  they share one interpreter. Naming the Python version twice once let a
  Renovate "non-major" bump move the runtime to 3.14 and leave the builder on
  3.12, producing an image with no site-packages on `sys.path`. OS patches,
  pip's removal and the uid 10001 user live there too, not here.
- `uv sync --no-editable`, or the runtime stage copies a venv whose `.pth` points
  at a build-stage path that does not exist.
- The `org.opencontainers.image.source` label is what links the package to this
  repository, and that link is what gives a workflow's `GITHUB_TOKEN` write
  access to it.
- **New GHCR packages default to private** regardless of repository visibility,
  and there is no pull secret. Flip it once after the first push.

## Heart rate zones are counted here, not taken from Garmin

The HR zone page and the insight prompt label zones as **heart rate reserve
(HRR)**, and the app makes that true itself: `garmin._zones` buckets each
activity's heart rate samples (`get_activity_details`, about one a second)
against Karvonen boundaries from Garmin Connect's DEFAULT `maxHeartRateUsed`
and that day's resting heart rate. Garmin's own `secsInZone` is not used: it is
bucketed on the watch against whatever zones the watch had, and the watch and
Garmin Connect disagreed about those (HRR on the watch, %max in Connect and in
every activity it recorded).

- Checked against Garmin's figures using Garmin's own boundaries: within
  seconds. Re-check that way before believing a change to the counting.
- **Ask for every sample** (`HR_SAMPLES` is a ceiling). Asked for fewer,
  Garmin thins them out rather than truncating; weighting by the gap to the
  next sample keeps that honest, but there is no reason to approximate.
- A gap over `HR_SAMPLE_MAX_GAP_SECONDS` is a pause and counts nowhere.
- The page shows every zone twice, **HR** (% of max) then **HRR**, both
  counted from the same samples and the same max. The HR set is
  `max_zone_seconds`/`max_zone_low_bpm`; `zone_seconds` stays HRR because the
  insight prompt reads it as HRR.
- Stored activities keep the zones they were counted with. Changing the
  maths or the max needs `gymlog garmin-sync --days 90` to recount them.

## The backup job: keep these true

`gymlog backup` (backup.py, main.container-apps-job.backup.tf) copies the log
and chat to R2, encrypted with age, and proves each copy decrypts and parses.
README's Backups section covers setup and restoring.

- **Copy bytes, not the model.** `store.load_raw` returns the blob exactly as
  stored. Round-tripping through `Log` would back up what *this* version
  writes, and a field it drops would be lost from every copy.
- **Never back up the Garmin session blob.** It is a login token, and the
  next sync makes a new one.
- **Never delete from the bucket here.** R2's lifecycle rules (cloudflare
  repo) are the only retention. The R2 token is Object Read & Write on that
  one bucket.
- **The owner's private key never goes near Azure or CI.** The drill key is
  separate, and readable only by what can already read the blob.
- The R2 keys and the drill key are in `selfcheck.BACKUP_SECRETS`, checked
  only in the job given `BACKUP_BUCKET`. Set them before the first release
  that has the job, or its selfcheck fails the smoke test and rolls back.
- **A rollback to a release from before the job** leaves the job running an
  image with no `backup` subcommand. It fails every night, and the
  job-failure alert says so, until a release with it deploys again.

## Commit messages

Conventional Commits, enforced by commitlint at commit-msg time. The
`no-commit-to-branch` hook blocks direct commits to `main`.

## Repo settings

Branch protection and repository settings are **not** configured here. This repo
is managed by [jay-withers/github-repos](https://github.com/jay-withers/github-repos).
Applying a required check that nothing reports leaves every PR *pending* rather
than failing it — check contexts against `gh pr checks` before applying.
