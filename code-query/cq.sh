#!/usr/bin/env bash
# cq.sh — Run the vendored code-query CLI from any repository.
#
# Resolves the interpreter, puts vendor/ on the import path, and forwards every
# argument to `python -m cq`.
#
# Usage:
#   ./cq.sh index
#   ./cq.sh search --q "resolve_run_id"
#   CQ_PROFILE=main ./cq.sh stats
#
# Environment:
#   CQ_PYTHON   Interpreter to use (default: .venv-cq if present, else python3).
#   CQ_PROFILE  Profile injected when the command line has no --profile.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENDOR_DIR="${SCRIPT_DIR}/vendor"
VENV_PY="${SCRIPT_DIR}/.venv-cq/bin/python"

if [ ! -f "${VENDOR_DIR}/cq/cli.py" ]; then
    echo "vendor/cq is missing. The kit was copied without its engine; re-copy the whole directory." >&2
    exit 2
fi

if [ -n "${CQ_PYTHON:-}" ]; then
    PYTHON="$CQ_PYTHON"
elif [ -x "$VENV_PY" ]; then
    PYTHON="$VENV_PY"
else
    PYTHON="python3"
fi

ARGS=("$@")
if [ -n "${CQ_PROFILE:-}" ]; then
    has_profile=0
    for arg in "${ARGS[@]:-}"; do
        if [ "$arg" = "--profile" ]; then has_profile=1; break; fi
    done
    if [ "$has_profile" -eq 0 ]; then
        ARGS+=("--profile" "$CQ_PROFILE")
    fi
fi

export PYTHONPATH="${VENDOR_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
exec "$PYTHON" -m cq "${ARGS[@]:-}"
