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
#   - /readyz answers 200.
#
# For a job, only the configured image: starting one has side effects.
#
# Shared verbatim by every app repo that deploys through container-apps-deploy
# (gym-log, finances, career, repo-agent, security-testing). Change it in all.
set -euo pipefail

if [[ $# -lt 4 ]]; then
  echo "usage: $0 <tag> <resource-group> <image-without-tag> app:<name>|job:<name>..." >&2
  exit 2
fi
tag="$1" rg="$2" image="$3:$1"
shift 3
timeout="${SMOKE_TIMEOUT:-300}"

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

  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 "https://$fqdn/readyz" || true)"
  [[ "$code" == 200 ]] || fail "$name: /readyz answered HTTP $code"
  echo "smoke: $name: $tag live and ready on https://$fqdn"
}

check_job() {
  local name="$1" configured
  configured="$(az containerapp job show -g "$rg" -n "$name" --query 'properties.template.containers[0].image' -o tsv)"
  [[ "$configured" == "$image" ]] || fail "job $name is configured with $configured, not $image"
  echo "smoke: job $name: configured with $tag"
}

for target in "$@"; do
  case "$target" in
    app:*) check_app "${target#app:}" ;;
    job:*) check_job "${target#job:}" ;;
    *) echo "smoke: unknown target $target (want app:<name> or job:<name>)" >&2; exit 2 ;;
  esac
done
