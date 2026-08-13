#!/usr/bin/env bash
# setup.sh — Linux/macOS launcher for the shared kit setup (FR-KIT-03).
#
# Every decision (dependency resolution, path decisions, configuration
# scaffolding, Skill placement) lives in kit/kit_setup.py. This file only
# resolves a bootstrap interpreter and forwards the arguments.
#
# Usage:
#   bash setup.sh --with-gui --install-skill
#   PYTHON=/usr/bin/python3.12 bash setup.sh --build-index
#
# Run `bash setup.sh --help` for the full option list.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"
ENTRY="${SCRIPT_DIR}/kit/kit_setup.py"

if [ ! -f "$ENTRY" ]; then
    echo "shared setup implementation not found: ${ENTRY}" >&2
    exit 2
fi

exec "$PYTHON" "$ENTRY" --kit-dir "$SCRIPT_DIR" --python "$PYTHON" "$@"
