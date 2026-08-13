#!/usr/bin/env bash
# mdq.sh — Run the vendored markdown-query CLI from any repository.
#
# Resolves the interpreter, puts vendor/ on the import path, and forwards every
# argument to `python -m mdq`.
#
# Usage:
#   ./mdq.sh index
#   ./mdq.sh search --q "requirement definition"
#
# Environment:
#   MDQ_PYTHON  Interpreter to use (default: .venv-mdq-gui if present, else python3).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENDOR_DIR="${SCRIPT_DIR}/vendor"
VENV_PY="${SCRIPT_DIR}/.venv-mdq-gui/bin/python"

if [ ! -f "${VENDOR_DIR}/mdq/cli.py" ]; then
    echo "vendor/mdq is missing. The kit was copied without its engine; re-copy the whole directory." >&2
    exit 2
fi

if [ -n "${MDQ_PYTHON:-}" ]; then
    PYTHON="$MDQ_PYTHON"
elif [ -x "$VENV_PY" ]; then
    PYTHON="$VENV_PY"
else
    PYTHON="python3"
fi

export PYTHONPATH="${VENDOR_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
exec "$PYTHON" -m mdq "$@"
