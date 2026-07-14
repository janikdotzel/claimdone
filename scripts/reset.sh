#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/runtime.sh"

claimdone_resolve_runtime
exec "$CLAIMDONE_PYTHON_BIN" "$CLAIMDONE_ROOT/scripts/reset.py"
