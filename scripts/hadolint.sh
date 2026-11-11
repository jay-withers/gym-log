#!/usr/bin/env bash
# Lints Dockerfiles with hadolint, via its container image.
#
# Wraps the upstream hadolint-docker hook rather than using it directly. That
# hook shells out to `docker system info` and buries an unreachable daemon in a
# wall of JSON; this says what is wrong instead.
#
# PROXY_SOCKET is the user-owned socat proxy the old dev-container images set
# up for a root-owned host socket. It is still honoured when present, and
# ignored otherwise (WSL with Docker Desktop, macOS, a CI runner).
set -euo pipefail

readonly PROXY_SOCKET=/var/run/docker-host.sock

if [[ -z "${DOCKER_HOST:-}" && -S "$PROXY_SOCKET" ]] && ! docker info >/dev/null 2>&1; then
  export DOCKER_HOST="unix://$PROXY_SOCKET"
fi

if ! docker info >/dev/null 2>&1; then
  echo "hadolint: cannot reach the Docker daemon." >&2
  echo "  Start Docker Desktop (on WSL, with WSL integration on for this distro)." >&2
  exit 1
fi

# --rm and stdin, so no image needs to know about the repository layout and
# nothing is left behind. One invocation per file keeps the reported path right.
status=0
for dockerfile in "$@"; do
  if ! docker run --rm -i ghcr.io/hadolint/hadolint:v2.15.1 \
      hadolint --no-color - < "$dockerfile" | sed "s|^-:|${dockerfile}:|"; then
    status=1
  fi
done
exit $status
