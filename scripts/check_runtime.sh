#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/runtime.sh"

claimdone_resolve_runtime
claimdone_print_runtime

uv_bin="$(claimdone_uv_bin)"
if [[ -x "$uv_bin" ]]; then
  actual_uv="$(claimdone_tool_version uv "$uv_bin")"
  [[ "$actual_uv" == "$CLAIMDONE_REQUIRED_UV" ]] \
    || claimdone_die "repo-local uv must be $CLAIMDONE_REQUIRED_UV, found $actual_uv"
  printf 'uv %s (repo-local)\n' "$actual_uv"
else
  printf 'uv %s will be bootstrapped by make setup\n' "$CLAIMDONE_REQUIRED_UV"
fi
