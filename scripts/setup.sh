#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/runtime.sh"

claimdone_resolve_runtime
claimdone_print_runtime
cd "$CLAIMDONE_ROOT"
bash "$CLAIMDONE_ROOT/scripts/bootstrap_uv.sh"

uv_bin="$(claimdone_uv_bin)"
uv_cache_dir="$(claimdone_uv_cache_dir)"
playwright_browsers_path="$(claimdone_playwright_browsers_path)"
mkdir -p "$uv_cache_dir" "$CLAIMDONE_ROOT/.local/state" "$CLAIMDONE_ROOT/.local/tmp"

"$CLAIMDONE_PNPM_BIN" install --frozen-lockfile --ignore-scripts
UV_CACHE_DIR="$uv_cache_dir" "$uv_bin" sync \
  --all-packages \
  --frozen \
  --no-build \
  --python "$CLAIMDONE_PYTHON_BIN"
PLAYWRIGHT_BROWSERS_PATH="$playwright_browsers_path" \
  UV_CACHE_DIR="$uv_cache_dir" \
  "$uv_bin" run --frozen --no-sync --package claimdone-api playwright install chromium
printf '%s\n' "$CLAIMDONE_REQUIRED_PLAYWRIGHT" \
  > "$playwright_browsers_path/.claimdone-ready"

printf 'ClaimDone dependencies are ready.\n'
