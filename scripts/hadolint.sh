#!/usr/bin/env bash
# Lints Dockerfiles with hadolint, via its container image.
#
# Wraps the upstream hadolint-docker hook rather than using it directly. That
# hook shells out to `docker system info`, which fails in this dev container
# whenever DOCKER_HOST is unset — and pre-commit invoked from `git commit`
# inherits a non-login shell, where /etc/profile.d/10-docker-host.sh has not
# run. The result was a blocked commit and a wall of JSON, depending on nothing
# more than how the shell was started.
#
# Shares the socket-resolution rule with the image's docker-socket-setup: the
# host socket is root-owned, so a user-owned socat proxy stands in for it.
set -euo pipefail

readonly PROXY_SOCKET=/var/run/docker-host.sock

if [[ -z "${DOCKER_HOST:-}" && -S "$PROXY_SOCKET" ]] && ! docker info >/dev/null 2>&1; then
  export DOCKER_HOST="unix://$PROXY_SOCKET"
fi

if ! docker info >/dev/null 2>&1; then
  echo "hadolint: cannot reach the Docker daemon." >&2
  echo "  In the dev container, DOCKER_HOST should point at $PROXY_SOCKET;" >&2
  echo "  a fresh terminal picks that up from /etc/profile.d/10-docker-host.sh." >&2
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
