#!/usr/bin/env bash
# Proves an image tag is live: `make smoke` after `make deploy`, locally or in
# the shared container-apps-deploy.yml, which rolls back when this fails.
#
#   smoke.sh <tag> <resource-group> <image-without-tag> app:<name>... job:<name>...
#
# A green `az containerapp update` only means Azure accepted the image. The
# apps scale to zero, so a revision whose container never starts can go live
# unnoticed until someone opens the page. So for each app this checks, on its
# default FQDN (the custom domain may not be bound yet):
#
#   - the latest revision is configured with the image;
#   - /healthz answers 200 and reports the tag as `version`, retrying through
#     a cold start. An image from before `version` existed is accepted once the
#     latest *ready* revision is the one running it, so rolling back to one
#     still passes;
#   - /readyz answers 200 (not checked on a rollback to an image without
#     `version`, which may predate it).
#
# For a job, the configured image, and then one run of it with its args
# swapped for `selfcheck`: the same image, identity, env and secrets as a real
# run, but the app's selfcheck command only loads its settings, reads its
# secrets and reaches its storage or database, and sends nothing. A real run
# would email, spend LLM tokens or trade. Every job's run is started before
# any is waited on. With SMOKE_PHASE=rollback (container-apps-deploy.yml sets
# it) the runs are skipped: the old image may predate `selfcheck`, it has
# already run here, and restoring service shouldn't wait on it.
#
# Shared verbatim by every app repo that deploys through container-apps-deploy
# (gym-log, finances, career, repo-agent, security-testing, market-agent).
# Change it in all.
set -euo pipefail

if [[ $# -lt 4 ]]; then
  echo "usage: $0 <tag> <resource-group> <image-without-tag> app:<name>|job:<name>..." >&2
  exit 2
fi
tag="$1" rg="$2" image="$3:$1"
shift 3
timeout="${SMOKE_TIMEOUT:-300}"
job_timeout="${SMOKE_JOB_TIMEOUT:-600}"
phase="${SMOKE_PHASE:-release}"
started=()

fail() {
  echo "smoke: FAIL: $*" >&2
  exit 1
}

check_app() {
  local name="$1" app fqdn latest ready configured deadline body code version
  app="$(az containerapp show -g "$rg" -n "$name" -o json)"
  configured="$(jq -r '.properties.template.containers[0].image' <<<"$app")"
  [[ "$configured" == "$image" ]] || fail "$name is configured with $configured, not $image"
  fqdn="$(jq -r '.properties.configuration.ingress.fqdn // empty' <<<"$app")"
  [[ -n "$fqdn" ]] || fail "$name has no ingress FQDN"
  latest="$(jq -r '.properties.latestRevisionName' <<<"$app")"

  deadline=$((SECONDS + timeout))
  while :; do
    version=""
    code="$(curl -sS -o /tmp/smoke-body.$$ -w '%{http_code}' --max-time 30 "https://$fqdn/healthz" || true)"
    body="$(cat /tmp/smoke-body.$$ 2>/dev/null || true)"
    if [[ "$code" == 200 ]]; then
      version="$(jq -r '.version // empty' <<<"$body" 2>/dev/null || true)"
      if [[ "$version" == "$tag" ]]; then
        break
      elif [[ -z "$version" ]]; then
        ready="$(az containerapp show -g "$rg" -n "$name" --query properties.latestReadyRevisionName -o tsv)"
        [[ "$ready" == "$latest" ]] && break
      fi
    fi
    ((SECONDS < deadline)) || fail "$name: /healthz never reported $tag within ${timeout}s (last: HTTP $code ${body:0:200})"
    echo "smoke: $name: waiting (HTTP $code${version:+, version $version})"
    sleep 10
  done
  rm -f /tmp/smoke-body.$$

  # Rolling back to an image from before `version`: it may predate /readyz too
  # (market-agent's dashboard did), and it has already run here.
  if [[ "$phase" == rollback && -z "$version" ]]; then
    echo "smoke: $name: $tag live (rollback to an image without version; /readyz not checked)"
    return 0
  fi

  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 "https://$fqdn/readyz" || true)"
  [[ "$code" == 200 ]] || fail "$name: /readyz answered HTTP $code"
  echo "smoke: $name: $tag live and ready on https://$fqdn"
}

check_job() {
  local name="$1" job configured body execution
  job="$(az containerapp job show -g "$rg" -n "$name" -o json)"
  configured="$(jq -r '.properties.template.containers[0].image' <<<"$job")"
  [[ "$configured" == "$image" ]] || fail "job $name is configured with $configured, not $image"
  echo "smoke: job $name: configured with $tag"
  [[ "$phase" != rollback ]] || return 0

  # The job's own template, args swapped. ARM's start API rather than
  # `az containerapp job start --args`, which builds the override from its
  # flags alone and drops the env (and with it every secret reference).
  body="$(jq -c '.properties.template | {
      containers: [.containers[] | {image, name, command, env, resources} + {args: ["selfcheck"]}],
      initContainers: [(.initContainers // [])[] | {image, name, command, args, env, resources}]
    } | (.containers, .initContainers) |= map(with_entries(select(.value != null)))' <<<"$job")"
  execution="$(az rest --method post \
    --url "https://management.azure.com$(jq -r .id <<<"$job")/start?api-version=2024-03-01" \
    --body "$body" --query name -o tsv)"
  [[ -n "$execution" ]] || fail "job $name: could not start a selfcheck run"
  echo "smoke: job $name: selfcheck run $execution started"
  started+=("$name/$execution")
}

wait_for_runs() {
  local entry name execution status deadline=$((SECONDS + job_timeout))
  for entry in "${started[@]}"; do
    name="${entry%%/*}" execution="${entry#*/}"
    while :; do
      status="$(az containerapp job execution show -g "$rg" -n "$name" \
        --job-execution-name "$execution" --query properties.status -o tsv)"
      case "$status" in
        Succeeded) echo "smoke: job $name: selfcheck run succeeded"; break ;;
        Failed | Stopped | Degraded)
          fail "job $name: selfcheck run $execution ended $status. Logs: az containerapp job logs show -g $rg -n $name --execution $execution --container <name>" ;;
      esac
      ((SECONDS < deadline)) || fail "job $name: selfcheck run $execution still $status after ${job_timeout}s"
      sleep 10
    done
  done
}

for target in "$@"; do
  case "$target" in
    app:*) check_app "${target#app:}" ;;
    job:*) check_job "${target#job:}" ;;
    *) echo "smoke: unknown target $target (want app:<name> or job:<name>)" >&2; exit 2 ;;
  esac
done
((${#started[@]} == 0)) || wait_for_runs
