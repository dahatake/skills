#!/usr/bin/env bash
# launch-gui.sh - Launch the standalone markdown-query GUI on Linux/macOS.
#
# Usage:
#   bash launch-gui.sh                    # operate on CWD
#   bash launch-gui.sh /path/to/repo      # operate on specific repo
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="${SCRIPT_DIR}/.venv-mdq-gui/bin/python"
LAUNCHER="${SCRIPT_DIR}/launch.py"

if [ ! -x "$VENV_PY" ]; then
    echo "[launch-gui] venv not found. Run: bash setup.sh" >&2
    exit 2
fi

exec "$VENV_PY" "$LAUNCHER" "$@"
