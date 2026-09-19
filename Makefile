TF_DIR := terraform
ENV ?= dev

IMAGE_REGISTRY ?= ghcr.io/jay-withers/gym-log
# Defaults to the local commit, which is what you want when iterating: build,
# push, and the tag you just built is the one you reference.
IMAGE_TAG ?= $(shell git rev-parse --short HEAD)
# `file` means the ?= default fired rather than the caller passing one.
IMAGE_TAG_EXPLICIT := $(filter-out file,$(origin IMAGE_TAG))

.DEFAULT_GOAL := help

.PHONY: help install lint test run seed build push deploy url logs import show init fmt validate plan apply secrets

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

# Expected to be re-run after a dev container rebuild, not just after a clone:
# uv installs into ~/.local/bin, which is the container's writable layer and
# does not survive one.
install: ## Install pre-commit hooks and Python dependencies
	pre-commit install
	pre-commit install --hook-type commit-msg
	command -v uv >/dev/null || curl -fsSL https://astral.sh/uv/install.sh | sh
	uv sync --extra dev

test: ## Run the test suite
	# --extra dev: pytest is an extra, not a dependency group, so `uv run` does
	# not install it. Without this the target only works after `make install`.
	uv run --extra dev pytest

lint: ## Run every pre-commit hook against every file
	pre-commit run --all-files

# No Azure at all: with no STATE_CONTAINER_URL the log falls back to a local
# file, and APP_PASSCODE is read from the environment before Key Vault is ever
# consulted. Browsers treat localhost as a secure origin, so the session cookie
# works over plain http here despite being set `Secure`.
run: ## Serve locally on :8000 against a local log file
	APP_PASSCODE=$${APP_PASSCODE:-local} uv run gymlog serve --reload

# The local file starts empty, which is the one state the app has least to show:
# no block, so no session screen, no suggestions and no history. This writes a
# sample log in exactly the shape the blob holds, with enough sessions to put
# every screen into a state worth looking at.
#
# STATE_CONTAINER_URL is cleared rather than merely expected to be unset: a
# developer with it exported for `make show` would otherwise get a refusal here,
# and the refusal is a guard against overwriting the real log, not a workflow.
# The hint is here rather than in the command's own refusal because the right
# answer depends on how you got here: `--force` if you ran the CLI, FORCE=1 if
# you ran make, and make cannot be passed `--force` as a target argument.
seed: ## Write a sample log to the local file (FORCE=1 replaces an existing one)
	@STATE_CONTAINER_URL= uv run gymlog seed $(if $(FORCE),--force,) || { \
		echo "hint: make seed FORCE=1   # replaces the existing local log" >&2; \
		exit 1; }

# The one-off that seeds a block from the spreadsheet. Against the real blob, so
# it needs Storage Blob Data Contributor on the container — which whoever
# applied the Terraform has.
import: ## Import a block from an .xlsx (FILE=path, NAME=label)
	@if [ -z "$(FILE)" ]; then echo "error: pass FILE=/path/to/Gym_3.xlsx" >&2; exit 1; fi
	STATE_CONTAINER_URL="$$(terraform -chdir=$(TF_DIR) output -raw state_container_url)" \
		uv run gymlog import "$(FILE)" --name "$(NAME)"

show: ## Print the deployed training log as JSON
	STATE_CONTAINER_URL="$$(terraform -chdir=$(TF_DIR) output -raw state_container_url)" \
		uv run gymlog show

# `--platform linux/amd64` is not optional. Container Apps runs amd64 only and
# this dev host is arm64, so a native build deploys an image that crash-loops
# with an exec format error and no other clue.
build: ## Build the image for linux/amd64 (set IMAGE_TAG, defaults to the git SHA)
	docker buildx build --platform linux/amd64 --load \
		-t $(IMAGE_REGISTRY)/gymlog:$(IMAGE_TAG) .

push: ## Push the image to ghcr.io (needs write:packages)
	gh auth token | docker login ghcr.io -u $$(gh api user --jq .login) --password-stdin
	docker push $(IMAGE_REGISTRY)/gymlog:$(IMAGE_TAG)

# A bare `make deploy` is a hard error, unlike build/push. Those default the tag
# to the local git SHA, which is what you want when iterating. Deploying is
# different: the default would silently roll the app onto whatever commit
# happens to be checked out, which may never have been pushed to ghcr.io at all.
deploy: ## Roll an image tag onto the app (IMAGE_TAG required)
	@if [ -z "$(IMAGE_TAG_EXPLICIT)" ]; then \
		echo "error: pass a tag explicitly, e.g. make deploy IMAGE_TAG=v0.1.0" >&2; exit 1; fi
	@case "$(IMAGE_TAG)" in latest|main|unset) \
		echo "error: $(IMAGE_TAG) is a moving tag. Container Apps only creates a revision when the template changes, so re-pushing one deploys nothing and reports success." >&2; exit 1;; esac
	terraform -chdir=$(TF_DIR) init -reconfigure -backend-config=backends/$(ENV).hcl
	az containerapp update \
		--name "$$(terraform -chdir=$(TF_DIR) output -raw container_app_name)" \
		--resource-group "$$(terraform -chdir=$(TF_DIR) output -raw resource_group_name)" \
		--image $(IMAGE_REGISTRY)/gymlog:$(IMAGE_TAG) \
		--set-env-vars IMAGE_TAG=$(IMAGE_TAG) \
		STATE_CONTAINER_URL="$$(terraform -chdir=$(TF_DIR) output -raw state_container_url)"

url: ## Print the application's URL
	@terraform -chdir=$(TF_DIR) output -raw app_url; echo

logs: ## Tail the deployed app's logs
	az containerapp logs show \
		--name "$$(terraform -chdir=$(TF_DIR) output -raw container_app_name)" \
		--resource-group "$$(terraform -chdir=$(TF_DIR) output -raw resource_group_name)" \
		--container gymlog --follow

secrets: ## Print the az command that populates this project's Key Vault
	@echo "az keyvault secret set --vault-name $$(terraform -chdir=$(TF_DIR) output -raw key_vault_name) --name APP-PASSCODE --value <passcode>"

init: ## terraform init, without configuring the state backend
	terraform -chdir=$(TF_DIR) init -backend=false

fmt: ## terraform fmt -recursive
	terraform -chdir=$(TF_DIR) fmt -recursive

validate: init ## terraform init + validate (no Azure credentials needed)
	terraform -chdir=$(TF_DIR) validate

plan: ## terraform init + plan
	terraform -chdir=$(TF_DIR) init -reconfigure -backend-config=backends/$(ENV).hcl
	terraform -chdir=$(TF_DIR) plan -var-file=environments/$(ENV).tfvars

apply: ## terraform init + apply
	terraform -chdir=$(TF_DIR) init -reconfigure -backend-config=backends/$(ENV).hcl
	terraform -chdir=$(TF_DIR) apply -var-file=environments/$(ENV).tfvars
