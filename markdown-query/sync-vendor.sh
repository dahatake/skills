#!/usr/bin/env bash
# sync-vendor.sh — Linux/macOS launcher for the shared kit sync (FR-KIT-03).
#
# Maintainer tool. The rules for what ships live in kit/kit_sync.py, which reads
# the engine and the canonical Skill definition out of the UPSTREAM repository,
# so point it there explicitly when running outside it:
#
#   bash sync-vendor.sh --source /upstream/mdq --repo-root /upstream
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"
ENTRY="${SCRIPT_DIR}/kit/kit_sync.py"

if [ ! -f "$ENTRY" ]; then
    echo "shared sync implementation not found: ${ENTRY}" >&2
    exit 2
fi

exec "$PYTHON" "$ENTRY" --kit-dir "$SCRIPT_DIR" "$@"
