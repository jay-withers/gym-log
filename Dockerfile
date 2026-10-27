# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e

# Container Apps runs linux/amd64 only, and the dev host here is arm64 (Docker
# Desktop on Apple Silicon). A native build produces an image that crash-loops
# with an exec format error and no other clue, so every build for deployment
# must pass --platform linux/amd64. The Makefile's `build` target does it rather
# than leaving it to memory.

# Builder and runtime both derive from `base`, and that is load-bearing rather
# than tidy. The runtime stage copies the whole virtualenv, and a venv is bound
# to the exact minor version that created it — its packages live in
# lib/python3.X/site-packages and no other interpreter will look there. In
# market-agent, while the stages named their Python separately, a Renovate
# "non-major" bump moved the runtime from 3.12 to 3.14 and left the builder on
# 3.12; the resulting image had no site-packages on sys.path at all and died
# with a bare "No module named". One FROM line means they cannot drift again.
FROM python:3.14-slim-bookworm@sha256:945c2676dc2b18e0c1f16879244122c57c0ffede60006bf3df159c507a31bcae AS base


FROM base AS builder

# uv is copied in rather than supplying the base image, which is exactly what
# lets the builder share the runtime's interpreter. UV_PYTHON_DOWNLOADS=never
# then stops uv fetching a different one of its own.
COPY --from=ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

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


FROM base AS runtime

# This label is what connects the pushed package to this repository, and that
# connection is what gives a workflow's GITHUB_TOKEN write access to it. Without
# it a package pushed by hand is user-scoped and unlinked, and cd-publish fails
# with `denied: permission_denied: write_package` however its `packages: write`
# permission is declared.
LABEL org.opencontainers.image.source="https://github.com/jay-withers/gym-log"

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Non-root, and no shell: nothing in here needs to log in. The uid is numeric
# and USER names the number, not the name — a runtime enforcing runAsNonRoot has
# to resolve the user before the container starts, and it cannot read this
# image's /etc/passwd to do it.
#
# The same layer removes the base image's pip, which vendors certifi, urllib3,
# msgpack and setuptools' pkg_resources at whatever versions shipped with it;
# Trivy reports them (a CRITICAL certifi among them). Nothing at runtime uses
# pip: the app runs from the venv copied in below, which uv built without one.
#
# The same layer applies Debian's published security fixes. The base image is
# rebuilt on its own schedule, and in between Trivy fails the build on a
# fixable HIGH/CRITICAL package (libssl3, CVE-2026-84782, was the first). The
# CI build caches layers by instruction, so OS_PATCH_DATE (today's date, passed
# by jay-withers/workflows' docker.yml) is declared just above it: this layer
# re-runs once a day and everything before it stays cached.
ARG OS_PATCH_DATE
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip uninstall --yes --root-user-action=ignore pip \
    && useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin gymlog

WORKDIR /app
COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv

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
