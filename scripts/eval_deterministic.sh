#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/runtime.sh"

claimdone_resolve_runtime
claimdone_require_project_environment
cd "$CLAIMDONE_ROOT"

uv_bin="$(claimdone_uv_bin)"
uv_cache_dir="$(claimdone_uv_cache_dir)"

UV_CACHE_DIR="$uv_cache_dir" \
PYTHONPATH="$CLAIMDONE_ROOT/services/api/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$uv_bin" run --frozen --no-sync --package claimdone-api \
  python -m evals.run_deterministic
