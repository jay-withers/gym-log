#!/usr/bin/env bash
# Runs tflint once per (directory, environment tfvars) pair under terraform/,
# so rules that depend on concrete variable values (naming, tags,
# region-specific checks) are evaluated against what each environment
# actually deploys, not just unresolved variables. Unlike checkov, tflint
# doesn't recurse into subdirectories on its own, so every directory
# containing .tf files is linted individually with the same shared config —
# today that's just the root configuration, but a future nested module or
# example is picked up without changing anything here. Both directories and
# environments are discovered dynamically: drop a new tfvars file in
# terraform/environments/ and it's covered automatically.
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
tf_dir="$repo_root/terraform"
tflint_config="$tf_dir/.tflint.hcl"

shopt -s nullglob
tfvars_files=("$tf_dir"/environments/*.tfvars)
shopt -u nullglob

if [ ${#tfvars_files[@]} -eq 0 ]; then
  echo "error: no tfvars files found in $tf_dir/environments" >&2
  exit 1
fi

declare -A seen_dirs
lint_dirs=()
while IFS= read -r -d '' file; do
  dir=$(dirname "$file")
  if [[ -z "${seen_dirs[$dir]:-}" ]]; then
    seen_dirs[$dir]=1
    lint_dirs+=("$dir")
  fi
done < <(find "$tf_dir" -type d -name '.?*' -prune -o -type f -name '*.tf' -print0)

tflint --init --chdir="$tf_dir" --config="$tflint_config"

status=0
for dir in "${lint_dirs[@]}"; do
  for tfvars in "${tfvars_files[@]}"; do
    env=$(basename "$tfvars" .tfvars)
    echo "==> tflint (${dir#"$repo_root"/}, $env)"
    if ! tflint --chdir="$dir" --config="$tflint_config" --var-file="$tfvars"; then
      status=1
    fi
  done
done

exit "$status"
