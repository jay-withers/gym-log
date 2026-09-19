#!/usr/bin/env bash
# Runs checkov against terraform/ once per environment tfvars file
# (terraform/environments/*.tfvars) so checks that depend on concrete
# variable values are evaluated against what each environment actually
# deploys, not just unresolved variables. Environments are discovered
# dynamically — add a new one by dropping a new tfvars file in
# terraform/environments/, no config changes needed here.
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
tf_dir="$repo_root/terraform"

shopt -s nullglob
tfvars_files=("$tf_dir"/environments/*.tfvars)
shopt -u nullglob

if [ ${#tfvars_files[@]} -eq 0 ]; then
  echo "error: no tfvars files found in $tf_dir/environments" >&2
  exit 1
fi

# Not passing --download-external-modules: it triggered checkov's external
# module resolution, which cost ~15s per invocation (~45s across 3
# environments) regardless of caching the download — that's checkov's own
# graph-building overhead, not network time. Azure/naming/azurerm has no
# resources of its own to scan, so the trade was pure cost for zero benefit
# here. Revisit if the module tree grows to include modules with real
# resources; checkov will keep logging a "Failed to download module" warning
# in the meantime, which is expected and harmless. (The naming module is still
# the only external module here — every resource this configuration creates is
# declared locally, so there's nothing external worth scanning.)

status=0
for tfvars in "${tfvars_files[@]}"; do
  env=$(basename "$tfvars" .tfvars)
  echo "==> checkov ($env)"
  if ! checkov -d "$tf_dir" --var-file "$tfvars" --quiet --compact; then
    status=1
  fi
done

exit "$status"
