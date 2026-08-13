#!/usr/bin/env bash
# Launch the standalone Code Query GUI on Linux/macOS.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="${SCRIPT_DIR}/.venv-cq/bin/python"
LAUNCHER="${SCRIPT_DIR}/launch.py"
if [ -n "${CQ_PYTHON:-}" ]; then
    PYTHON="$CQ_PYTHON"
elif [ -x "$VENV_PY" ]; then
    PYTHON="$VENV_PY"
else
    PYTHON="${PYTHON:-python3}"
fi
exec "$PYTHON" "$LAUNCHER" "$@"
