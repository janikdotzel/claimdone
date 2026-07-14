#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/runtime.sh"

claimdone_resolve_runtime
claimdone_require_project_environment
cd "$CLAIMDONE_ROOT"

uv_bin="$(claimdone_uv_bin)"
uv_cache_dir="$(claimdone_uv_cache_dir)"
web_pid=""
api_pid=""

cleanup() {
  local status=$?
  local pid

  trap - EXIT INT TERM HUP
  for pid in "$web_pid" "$api_pid"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  for pid in "$web_pid" "$api_pid"; do
    if [[ -n "$pid" ]]; then
      wait "$pid" 2>/dev/null || true
    fi
  done
  exit "$status"
}

trap cleanup EXIT
trap 'exit 130' INT TERM HUP

printf 'Web: http://127.0.0.1:3000\n'
printf 'API: http://127.0.0.1:8000\n'

"$CLAIMDONE_PNPM_BIN" dev:web &
web_pid=$!
UV_CACHE_DIR="$uv_cache_dir" "$uv_bin" run --frozen --no-sync \
  --package claimdone-api uvicorn \
  --app-dir services/api/src claimdone_api.main:app \
  --host 127.0.0.1 --port 8000 &
api_pid=$!

status=0
while true; do
  if ! kill -0 "$web_pid" 2>/dev/null; then
    wait "$web_pid" || status=$?
    break
  fi
  if ! kill -0 "$api_pid" 2>/dev/null; then
    wait "$api_pid" || status=$?
    break
  fi
  sleep 1
done

exit "$status"
