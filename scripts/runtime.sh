#!/usr/bin/env bash

CLAIMDONE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
CLAIMDONE_REQUIRED_NODE="24.14.0"
CLAIMDONE_REQUIRED_PNPM="11.7.0"
CLAIMDONE_REQUIRED_PYTHON="3.12.13"
CLAIMDONE_REQUIRED_UV="0.8.3"
CLAIMDONE_REQUIRED_PLAYWRIGHT="1.61.0"
export NEXT_TELEMETRY_DISABLED=1

claimdone_die() {
  printf 'ClaimDone tooling error: %s\n' "$*" >&2
  exit 1
}

claimdone_tool_version() {
  local kind="$1"
  local binary="$2"
  local raw

  raw="$("$binary" --version 2>&1)" || return 1
  case "$kind" in
    node)
      raw="${raw#v}"
      ;;
    python)
      raw="${raw#Python }"
      ;;
    uv)
      raw="${raw#uv }"
      ;;
  esac
  printf '%s\n' "${raw%% *}"
}

claimdone_pick_exact_tool() {
  local label="$1"
  local kind="$2"
  local expected="$3"
  local override="$4"
  local path_candidate="$5"
  local bundled_candidate="$6"
  local candidate
  local actual

  if [[ -n "$override" ]]; then
    [[ -x "$override" ]] || claimdone_die "$label override is not executable"
    actual="$(claimdone_tool_version "$kind" "$override")" || claimdone_die "$label override failed"
    [[ "$actual" == "$expected" ]] || claimdone_die "$label must be $expected, found $actual"
    printf '%s\n' "$override"
    return
  fi

  for candidate in "$path_candidate" "$bundled_candidate"; do
    [[ -n "$candidate" && -x "$candidate" ]] || continue
    actual="$(claimdone_tool_version "$kind" "$candidate")" || continue
    if [[ "$actual" == "$expected" ]]; then
      printf '%s\n' "$candidate"
      return
    fi
  done

  claimdone_die "$label $expected was not found; install it or set the documented override"
}

claimdone_resolve_runtime() {
  local dependencies_root
  local path_node
  local path_pnpm
  local path_python

  dependencies_root="${CODEX_PRIMARY_RUNTIME_DEPS:-${HOME:-}/.cache/codex-runtimes/codex-primary-runtime/dependencies}"
  path_node="$(command -v node 2>/dev/null || true)"
  CLAIMDONE_NODE_BIN="$(claimdone_pick_exact_tool \
    "Node.js" "node" "$CLAIMDONE_REQUIRED_NODE" "${CLAIMDONE_NODE_BIN:-}" \
    "$path_node" "$dependencies_root/node/bin/node")"
  export CLAIMDONE_NODE_BIN
  export PATH="$(dirname "$CLAIMDONE_NODE_BIN"):$PATH"

  path_pnpm="$(command -v pnpm 2>/dev/null || true)"
  CLAIMDONE_PNPM_BIN="$(claimdone_pick_exact_tool \
    "pnpm" "pnpm" "$CLAIMDONE_REQUIRED_PNPM" "${CLAIMDONE_PNPM_BIN:-}" \
    "$path_pnpm" "$dependencies_root/bin/fallback/pnpm")"
  export CLAIMDONE_PNPM_BIN
  export PATH="$(dirname "$CLAIMDONE_PNPM_BIN"):$PATH"

  path_python="$(command -v python3 2>/dev/null || true)"
  CLAIMDONE_PYTHON_BIN="$(claimdone_pick_exact_tool \
    "Python" "python" "$CLAIMDONE_REQUIRED_PYTHON" "${CLAIMDONE_PYTHON_BIN:-}" \
    "$path_python" "$dependencies_root/python/bin/python3")"
  export CLAIMDONE_PYTHON_BIN
}

claimdone_uv_bin() {
  printf '%s\n' "$CLAIMDONE_ROOT/.tools/uv/$CLAIMDONE_REQUIRED_UV/bin/uv"
}

claimdone_uv_cache_dir() {
  printf '%s\n' "$CLAIMDONE_ROOT/.tools/cache/uv"
}

claimdone_playwright_browsers_path() {
  printf '%s\n' "${CLAIMDONE_PLAYWRIGHT_BROWSERS_PATH:-$CLAIMDONE_ROOT/.tools/playwright}"
}

claimdone_print_runtime() {
  printf 'Node.js %s\n' "$(claimdone_tool_version node "$CLAIMDONE_NODE_BIN")"
  printf 'pnpm %s\n' "$(claimdone_tool_version pnpm "$CLAIMDONE_PNPM_BIN")"
  printf 'Python %s\n' "$(claimdone_tool_version python "$CLAIMDONE_PYTHON_BIN")"
}

claimdone_require_project_environment() {
  local uv_bin
  local playwright_marker
  local playwright_browsers_path
  uv_bin="$(claimdone_uv_bin)"
  playwright_browsers_path="$(claimdone_playwright_browsers_path)"
  playwright_marker="$playwright_browsers_path/.claimdone-ready"

  [[ -x "$uv_bin" ]] || claimdone_die "repo-local uv is missing; run make setup"
  [[ "$(claimdone_tool_version uv "$uv_bin")" == "$CLAIMDONE_REQUIRED_UV" ]] \
    || claimdone_die "repo-local uv has the wrong version; run make setup"
  [[ -d "$CLAIMDONE_ROOT/node_modules" ]] || claimdone_die "node_modules is missing; run make setup"
  [[ -d "$CLAIMDONE_ROOT/.venv" ]] || claimdone_die ".venv is missing; run make setup"
  [[ -f "$playwright_marker" ]] \
    || claimdone_die "Playwright Chromium is missing; run make setup"
  [[ "$(<"$playwright_marker")" == "$CLAIMDONE_REQUIRED_PLAYWRIGHT" ]] \
    || claimdone_die "Playwright Chromium has the wrong version; run make setup"
  export PLAYWRIGHT_BROWSERS_PATH="$playwright_browsers_path"
}
