# gym-log

A training log for two full-body sessions a week, running on the shared Azure
Container Apps environment. It replaces a spreadsheet, and it tells you what to
lift next.

## Why it exists

`Gym_3.xlsx` had two sheets, `Tues` and `Thur`, seven slots each — chest, back,
legs, shoulders, triceps, biceps and a finisher — with prescribed sets, a rep
range, a rest interval, and one achieved-reps and weight cell per exercise.

Two things were wrong with it:

- **It kept no history.** The achieved cells were overwritten every session.
  `Gym_3` implies a `Gym_1` and a `Gym_2`; whatever they held is gone. There was
  no way to ask whether the chest slot had moved in six months.
- **It did no work for you.** The `Rest` column was text that nothing counted
  down, and next session's load came out of memory.

So: every set is appended and never overwritten, the next load is derived from
the last session rather than recalled, and rotating the exercises every eight
weeks adds to the record instead of resetting it.

## How it decides what to suggest

Double progression, which is what the rep ranges in the sheet already implied
without acting on:

| Last session | Next |
| --- | --- |
| every set at or above the top of the range | add the exercise's increment, back to the bottom of the range |
| any set below the bottom of the range | hold the weight, and say so |
| anything else | hold the weight, one more rep |

The **weakest** set decides, not the average — a strong first set hiding two that
fell short is exactly the self-deception a single overwritten cell allowed. The
increment is per-exercise, because 7.5, 55, 24 and 5 all came off the same sheet
and a cable stack, a machine and a dumbbell rack do not share a step.

`src/gymlog/progression.py` is pure, has no I/O, and is where to look.

## Slots, not exercises

A **slot** (chest, back, ...) is stable; the exercise filling it is not. Every
logged entry carries its slot, so `/history/chest` spans the rotation:

```
2026-10-28   Incline DB Press          11 × 20kg
2026-09-15   Low-to-High Cable Flyes   12 × 7.5kg
```

That is the one thing the spreadsheet could not do, and the reason the model is
shaped the way it is.

## Beyond the lift log

The lift log itself — block/day, session logging, history, block rotation —
now lives under `/strength` rather than at the root. `/` is a light home page
linking to it and to the sections below; the installed-to-home-screen phone
icon still opens straight to `/strength`, since that is the twice-a-week fast
path this app exists for.

Four sections that are not derived from sets and reps:

- **Achievements** (`/achievements`) — a free-text, append-only list of
  milestones. Never edited, only added to.
- **Goals** (`/goals`) — free-form targets with an optional date, marked
  achieved by hand. Not tied to a specific exercise or weight.
- **Injuries & conditions** (`/conditions`) — structured records (body part,
  status, start/resolved dates, a note), marked resolved by hand. Read by the
  insight so it does not suggest anything that would aggravate an active one.
- **Insight** (`/insights`) — a DeepSeek-generated summary of the week,
  written on a weekly schedule by `azurerm_container_app_job.insight`
  (`terraform/main.container-apps-job.tf`). The app scales to zero between
  workouts, so nothing in-process could fire a weekly timer; the job exists
  specifically to run on a schedule regardless of whether anyone has opened
  the app that week. There is no cooldown between runs, so the page also has
  a "Generate insight now" button that calls DeepSeek in-process and blocks
  the request for the few seconds that takes; `make insight` runs the same
  generation by hand against the real log, and `az containerapp job start`
  triggers it on the deployed job directly.

## Design

- **A single container app**, `min_replicas = 0`, on the shared environment from
  [jay-withers/azure-container-apps](https://github.com/jay-withers/azure-container-apps).
  It is resolved by name through a data source; this repo has no access to the
  platform's Terraform state.
- **One JSON blob** holds everything. Two sessions a week is about 2 KB, so a
  year is about 100 KB — there is no query to serve and nothing to justify a
  database. market-agent's PostgreSQL bills ~£13/month whether used or not, and
  was forced there by a policy blocking Azure SQL rather than chosen.
- **Server-rendered Jinja2**, no build step, no SPA, one image.
- **A passcode, then a signed cookie** valid for thirty days. Entra sign-in would
  need the `azapi` provider, which nothing in this estate uses, in exchange for a
  redirect on a phone between sets.

### Cost

| | Idle cost |
| --- | --- |
| Container app | £0 — `min_replicas = 0`, Consumption plan |
| Storage account | Pennies — about 100 KB a year |
| Key Vault | Effectively £0 — priced per operation |
| Log Analytics / App Insights | £0, inside the platform's shared free grant |
| Managed identity, resource group | Free |

**The cold start is paid once per workout, not once per set.** Open it before the
first exercise and wait; the replica stays warm for the hour it is being used.
Keeping a replica standing would cost roughly £11/month to avoid that, on a
platform whose entire design is that nothing bills while idle.

## Using it

`make url` prints the address. It is stable across deploys — it comes from the
app name and the shared environment, not from the revision — so bookmark it and
add it to the phone's home screen, where the manifest opens it without browser
chrome.

## Local development

No Azure involved: with no `STATE_CONTAINER_URL` the log falls back to a local
file, and `settings.py` reads the passcode from the environment before Key Vault.

```bash
make install
make test
make seed         # a sample log, shaped exactly like the one in blob storage
make run          # http://localhost:8000, passcode "local"
```

`make seed` matters more than it sounds: an empty local file leaves the app with
no block, and therefore no session screen, no suggestions and no history — the
one state with least to look at. The sample carries a block part-way through and
five sessions chosen to put every card into a different state (climbing inside
the range, ready to add weight, stalled below it, and never logged), so a change
to the session screen can be seen rather than imagined. The same sample also
seeds an achievement, a goal in each of active/achieved, a condition in each of
active/resolved, and a weekly insight, so those four sections are never the
one blank thing in an otherwise-populated local run. `make seed FORCE=1`
replaces an existing one (make cannot take `--force` as a target argument).

It writes to the local file only, and **refuses outright when
`STATE_CONTAINER_URL` is set** — invented sessions must never reach the real log.

`make insight-local` regenerates the weekly insight against that same local
file — set `DEEPSEEK_API_KEY` in the environment first, since there is no Key
Vault to fall back to locally. Without a key it fails with the same
`secret()` error a missing `APP_PASSCODE` would.

Browsers treat `localhost` as a secure origin, so the `Secure` session cookie
works over plain http there. `httpx` does not, which is why the tests point
`TestClient` at `https://testserver`.

### Seeding a block

```bash
gymlog import Gym_3.xlsx --name "Block 3"    # locally
make import FILE=/path/to/Gym_3.xlsx NAME="Block 3"   # against the real blob
```

It imports the **prescription** — exercise names, sets, rep range, rest — and the
recorded weight as a seed so the first suggestion is not blind. It deliberately
does not turn the achieved-reps cells into a logged session: they carry no date,
and inventing one would put a lie at the head of the history.

## Deploying

```bash
make build IMAGE_TAG=v0.1.0
make push  IMAGE_TAG=v0.1.0
make deploy IMAGE_TAG=v0.1.0
```

`terraform apply` seeds the first revision only. After that the image and env sit
under `ignore_changes` and `make deploy` (az cli) owns them, so a plan reports no
change even when the running image has moved on.

**The tag must be immutable.** Container Apps creates a revision only when the
template changes, so re-pushing `latest` deploys nothing and reports success.

### The one rule about `command`

**The container sets no `command`.** The Dockerfile's `ENTRYPOINT` owns the
executable name and `args = ["serve"]` picks the subcommand.

market-agent took a full outage from the opposite arrangement in September 2026:
`command = ["marketagent"]` sat under `lifecycle { ignore_changes }`, went stale
when the console script was renamed, and every workload crash-looped on
`exec: "investagent": executable file not found`. The Terraform had already been
corrected; the ignore meant no apply ever pushed it.

## First-time setup

Ordering-sensitive:

1. In [jay-withers/github-repos](https://github.com/jay-withers/github-repos):
   add `gym-log` to `scripts/bootstrap-state.ps1`'s `containers.json` and re-run
   it, **then** add the repository and `state_consumers` entries to
   `terraform/terraform.tfvars`, then apply. The container must exist before the
   data source looks it up.
2. Set `AZURE_CLIENT_ID`, `AZURE_TENANT_ID` and `AZURE_SUBSCRIPTION_ID` as
   repository variables, or the plan legs stay skipped.
3. `make apply`.
4. `make secrets` and run what it prints to set `APP-PASSCODE` and
   `DEEPSEEK-API-KEY`. The insight job fails at the next scheduled run,
   rather than at apply time, if the latter is skipped — it resolves the key
   the same way `APP-PASSCODE` is resolved, lazily and at runtime.
5. **Flip the GHCR package to public.** New packages default to private
   regardless of repository visibility, and there is no pull secret.
6. `make import FILE=Gym_3.xlsx NAME="Block 3"`.
7. `make deploy IMAGE_TAG=v0.0.1`.

## CI

Every workflow is a thin caller of a reusable workflow in
[jay-withers/workflows](https://github.com/jay-withers/workflows), pinned by
commit SHA with the tag as a comment. Required checks are set in `github-repos`,
not here: `pre-commit / Pre-commit`, `test / Test` and `terraform / Terraform`.
Read them off `gh pr checks` rather than inferring them. Terraform plans are
run manually with Azure access because the shared platform environment is
resolved through a data source.

Generated Terraform reference: [`terraform/README.md`](terraform/README.md).
