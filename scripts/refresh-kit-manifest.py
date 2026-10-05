#!/usr/bin/env python3
"""Recompute the SHA-256 manifest of a distribution kit.

`KIT-VERSION.json` records a hash per shipped file, and
`install.py --kit-dir <kit> --verify` compares them. Whenever a kit is patched in
this repository the manifest has to be regenerated, otherwise `--verify` reports
the fix as tampering.

    python scripts/refresh-kit-manifest.py markdown-query code-query
    python scripts/refresh-kit-manifest.py --check markdown-query
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path, PurePosixPath

MANIFEST_NAME = "KIT-VERSION.json"
SKIP_DIR_PREFIXES = (".venv",)
# `results` holds benchmark and evaluation output produced inside the kit.
SKIP_DIR_NAMES = {"__pycache__", "results", ".mdq", ".cq", ".git"}


def shipped_files(kit: Path) -> list[str]:
    found = []
    for path in kit.rglob("*"):
        if not path.is_file():
            continue
        parts = path.relative_to(kit).parts
        if any(p in SKIP_DIR_NAMES or p.startswith(SKIP_DIR_PREFIXES) for p in parts[:-1]):
            continue
        rel = PurePosixPath(*parts).as_posix()
        if rel == MANIFEST_NAME:
            continue
        found.append(rel)
    # The lowercased key reproduces the ordering the upstream manifest used; the
    # raw path breaks ties so the output is identical on a case-sensitive
    # filesystem too.
    return sorted(
        found,
        key=lambda rel: (tuple(p.lower() for p in PurePosixPath(rel).parts), rel),
    )


def build(kit: Path) -> dict:
    path = kit / MANIFEST_NAME
    manifest = json.loads(path.read_text(encoding="utf-8"))
    files = shipped_files(kit)
    manifest["file_count"] = len(files)
    manifest["files"] = {
        rel: hashlib.sha256((kit / rel).read_bytes()).hexdigest() for rel in files
    }
    return manifest


def render(manifest: dict) -> bytes:
    return (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kits", nargs="+", type=Path)
    parser.add_argument("--check", action="store_true", help="verify without writing")
    args = parser.parse_args(argv)

    status = 0
    for kit in args.kits:
        kit = kit.resolve()
        target = kit / MANIFEST_NAME
        if not target.is_file():
            print(f"{kit}: {MANIFEST_NAME} not found", file=sys.stderr)
            status = 2
            continue
        rendered = render(build(kit))
        if args.check:
            if target.read_bytes() != rendered:
                print(f"{kit.name}: {MANIFEST_NAME} is stale", file=sys.stderr)
                status = max(status, 1)
            else:
                print(f"{kit.name}: {MANIFEST_NAME} is current")
            continue
        changed = target.read_bytes() != rendered
        target.write_bytes(rendered)
        print(f"{kit.name}: {'updated' if changed else 'unchanged'} ({json.loads(rendered)['file_count']} files)")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
