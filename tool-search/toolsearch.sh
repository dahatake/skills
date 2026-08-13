#!/usr/bin/env bash
# toolsearch.sh — Run the vendored Tool Search CLI from any repository.
#
# Resolves the interpreter, puts vendor/ on the import path, and forwards every
# argument to `python -m toolsearch`.
#
# Usage:
#   ./toolsearch.sh dashboard
#   ./toolsearch.sh dashboard --html tool-search.html
#   ./toolsearch.sh skills --repo-root .
#
# Environment:
#   TOOLSEARCH_PYTHON  Interpreter to use (default: .venv-toolsearch if present, else python3).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENDOR_DIR="${SCRIPT_DIR}/vendor"
VENV_PY="${SCRIPT_DIR}/.venv-toolsearch/bin/python"

if [ ! -f "${VENDOR_DIR}/toolsearch/cli.py" ]; then
    echo "vendor/toolsearch is missing. The kit was copied without its engine; re-copy the whole directory." >&2
    exit 2
fi

if [ -n "${TOOLSEARCH_PYTHON:-}" ]; then
    PYTHON="$TOOLSEARCH_PYTHON"
elif [ -x "$VENV_PY" ]; then
    PYTHON="$VENV_PY"
else
    PYTHON="python3"
fi

export PYTHONPATH="${VENDOR_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
exec "$PYTHON" -m toolsearch "$@"
