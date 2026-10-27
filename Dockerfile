# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e

# Container Apps runs linux/amd64 only, and the dev host here is arm64 (Docker
# Desktop on Apple Silicon). A native build produces an image that crash-loops
# with an exec format error and no other clue, so every build for deployment
# must pass --platform linux/amd64. The Makefile's `build` target does it rather
# than leaving it to memory.

# Both stages come from jay-withers/dev-containers, which holds what every
# Python app here used to repeat: the Python digest, Debian security updates,
# pip's removal, the uid 10001 user, and uv for the builder. Keep the two at the
# same version — Renovate raises them together. python-app-builder is built FROM
# python-app, and that is load-bearing rather than tidy: the runtime stage
# copies the whole virtualenv, and a venv is bound to the exact minor version
# that created it. In market-agent, while the stages named their Python
# separately, a Renovate "non-major" bump moved the runtime from 3.12 to 3.14
# and left the builder on 3.12; the resulting image had no site-packages on
# sys.path at all and died with a bare "No module named".
FROM ghcr.io/jay-withers/dev-containers/python-app-builder:v0.18.0@sha256:515220599b90696ee950a19f115b2b5be9fb27e847103b416e5661ec21d9b23e AS builder

# python-app already sets this; restated because the relative COPYs below
# depend on it, and hadolint cannot see a WORKDIR set by the parent image.
WORKDIR /app

# Dependencies before source, so editing a module does not re-resolve the whole
# environment. --frozen fails rather than silently updating uv.lock, which is
# what makes the image reproducible.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --no-editable

COPY src ./src
# --no-editable matters: without it uv installs the project as a link back to
# /app/src, and the runtime stage copies only the virtualenv — leaving a .pth
# pointing at a directory that does not exist there. The failure is a bare
# "No module named 'gymlog'" from an image whose venv looks complete.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable


# Brings /app/.venv/bin on PATH and WORKDIR /app.
FROM ghcr.io/jay-withers/dev-containers/python-app:v0.18.0@sha256:6218e3ffc531d9c411a5be2ef3b3a5988e1c279c557d257a8594394810db054c

# This label is what connects the pushed package to this repository, and that
# connection is what gives a workflow's GITHUB_TOKEN write access to it. Without
# it a package pushed by hand is user-scoped and unlinked, and cd-publish fails
# with `denied: permission_denied: write_package` however its `packages: write`
# permission is declared. It also replaces the label python-app inherits, which
# would otherwise link this image to dev-containers.
LABEL org.opencontainers.image.source="https://github.com/jay-withers/gym-log"

COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv

# python-app already runs as this user; restated so this file alone says the
# image is non-root (repo-agent's container.runs_as_root reads only this file)
# and stays so if the base ever changes. Numeric, because a runtime enforcing
# runAsNonRoot has to resolve the user without reading /etc/passwd.
USER 10001

EXPOSE 8000

# The inverse of repo-agent, whose Dockerfile explains the absence of these two
# lines by saying "this image serves nothing". This one does serve something.
#
# Kept in the image so `docker run` checks exactly what Container Apps' liveness
# probe checks. /healthz rather than /readyz deliberately: this decides whether
# to restart the container, and restarting it does not fix unreachable storage.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"]

# **This ENTRYPOINT is the single source of truth for the executable's name.**
# Terraform deliberately sets no `command` on the container app — it sets `args`
# only — so that renaming the console script is one atomic change here rather
# than a change that has to be mirrored in another repository's Terraform. See
# terraform/main.container-apps.tf for the outage that taught this.
ENTRYPOINT ["gymlog"]
CMD ["serve"]
