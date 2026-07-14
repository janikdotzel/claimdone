#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/runtime.sh"

mode="${1:-}"
claimdone_resolve_runtime
claimdone_require_project_environment
cd "$CLAIMDONE_ROOT"

uv_bin="$(claimdone_uv_bin)"
uv_cache_dir="$(claimdone_uv_cache_dir)"

case "$mode" in
  lint)
    for script in "$CLAIMDONE_ROOT"/scripts/*.sh; do
      /bin/bash -n "$script"
    done
    "$CLAIMDONE_PNPM_BIN" lint:web
    UV_CACHE_DIR="$uv_cache_dir" "$uv_bin" run --frozen --no-sync \
      --package claimdone-api ruff check services/api scripts/reset.py scripts/tests
    ;;
  typecheck)
    "$CLAIMDONE_PNPM_BIN" typecheck:web
    UV_CACHE_DIR="$uv_cache_dir" "$uv_bin" run --frozen --no-sync \
      --package claimdone-api mypy \
      services/api/src services/api/tests scripts/reset.py scripts/tests
    ;;
  test)
    "$CLAIMDONE_PNPM_BIN" test:web
    UV_CACHE_DIR="$uv_cache_dir" "$uv_bin" run --frozen --no-sync \
      --package claimdone-api pytest
    ;;
  *)
    claimdone_die "verify mode must be one of: lint, typecheck, test"
    ;;
esac
