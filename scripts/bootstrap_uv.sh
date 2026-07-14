#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/runtime.sh"

claimdone_resolve_runtime

uv_bin="$(claimdone_uv_bin)"
uv_home="$(dirname "$(dirname "$uv_bin")")"

if [[ -x "$uv_bin" ]] \
  && [[ "$(claimdone_tool_version uv "$uv_bin")" == "$CLAIMDONE_REQUIRED_UV" ]]; then
  printf 'uv %s already available in the repo-local tool directory\n' "$CLAIMDONE_REQUIRED_UV"
  exit 0
fi

mkdir -p "$(dirname "$uv_home")"
"$CLAIMDONE_PYTHON_BIN" -m venv "$uv_home"
PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 \
  "$uv_home/bin/python" -m pip install \
  --no-deps \
  --only-binary=:all: \
  "uv==$CLAIMDONE_REQUIRED_UV"

actual_uv="$(claimdone_tool_version uv "$uv_bin")"
[[ "$actual_uv" == "$CLAIMDONE_REQUIRED_UV" ]] \
  || claimdone_die "uv bootstrap produced $actual_uv instead of $CLAIMDONE_REQUIRED_UV"
printf 'Bootstrapped uv %s in the repo-local tool directory\n' "$actual_uv"
