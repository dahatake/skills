"""Shared regeneration logic for the Skill distribution kits (FR-KIT-03).

Maintainer tool. It reads the engine and the canonical Skill definition out of
the UPSTREAM repository, so both have to be pointed at explicitly when this runs
anywhere else. The per-OS launchers (`sync-vendor.ps1` / `sync-vendor.sh`) only
delegate here, so the rules for what ships exist in exactly one place.

    python <kit>/kit/kit_sync.py --kit-dir <kit> \\
        --source /upstream/cq --repo-root /upstream
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Sequence

# Repository-specific and non-portable material must not ship with an engine.
# Directory names are matched at **any** depth: a GUI package under the engine
# carries its own test package, and generated interpreter/type-checker caches
# must stay upstream.
DROP_DIR_NAMES = ("tests", "__pycache__", ".mypy_cache")
DROP_FILE_NAMES = ("golden-queries.json", "golden-queries-holdout.json")

# Skill appendices that only make sense inside the upstream repository.
SKILL_REPO_SPECIFIC_DIR = "repo-specific"


def load_manifest(kit_dir: Path) -> dict:
    path = kit_dir / "kit.toml"
    if not path.is_file():
        raise SystemExit(f"kit manifest not found: {path}")
    return tomllib.loads(path.read_text(encoding="utf-8"))["kit"]


def _prune(root: Path) -> None:
    # Deepest first so removing a parent cannot invalidate a queued child path.
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and path.name in DROP_DIR_NAMES:
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_file() and path.name in DROP_FILE_NAMES:
            path.unlink()


def _replace_tree(source: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)


def sync_engine(kit_dir: Path, engine: str, source: Path) -> Path:
    """Copy the upstream engine package into ``<kit>/vendor/<engine>``."""
    if not (source / "cli.py").is_file():
        raise SystemExit(
            f"upstream {engine} package not found at {source}. Pass --source <path>."
        )
    target = kit_dir / "vendor" / engine
    _replace_tree(source, target)
    _prune(target)
    return target


def sync_skill(kit_dir: Path, repo_root: Path, skill: str) -> Path:
    """Copy the canonical Skill definition into ``<kit>/skill``."""
    source = repo_root / ".github" / "skills" / skill
    if not source.is_dir():
        raise SystemExit(f"canonical skill definition not found: {source}")
    target = kit_dir / "skill"
    _replace_tree(source, target)
    repo_specific = target / "references" / SKILL_REPO_SPECIFIC_DIR
    if repo_specific.exists():
        shutil.rmtree(repo_specific)
    _prune(target)
    return target


def sync_shared(kit_dir: Path, shared_dir: Path) -> Path:
    """Copy this shared implementation into ``<kit>/kit`` for distribution."""
    target = kit_dir / "kit"
    _replace_tree(shared_dir, target)
    _prune(target)
    return target


def _count_files(root: Path) -> int:
    return sum(1 for p in root.rglob("*") if p.is_file())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit-dir", type=Path, required=True)
    parser.add_argument(
        "--source", type=Path, default=None,
        help="Upstream engine package directory (default: <repo-root>/<engine>).",
    )
    parser.add_argument(
        "--repo-root", type=Path, default=None,
        help="Upstream repository root (default: inferred from --kit-dir).",
    )
    args = parser.parse_args(argv)

    kit_dir = args.kit_dir.resolve()
    manifest = load_manifest(kit_dir)
    engine = manifest["engine"]
    skill = manifest["skill"]

    shared_dir = Path(__file__).resolve().parent
    repo_root = (
        args.repo_root.resolve() if args.repo_root else kit_dir.parents[2]
    )
    source = (args.source.resolve() if args.source else repo_root / engine)

    label = f"[{skill} sync]"
    print(f"{label} source: {source}")
    vendor = sync_engine(kit_dir, engine, source)
    print(f"{label} vendored {_count_files(vendor)} file(s) into {vendor}")

    skill_target = sync_skill(kit_dir, repo_root, skill)
    print(f"{label} skill definition copied into {skill_target}")

    shared_target = sync_shared(kit_dir, shared_dir)
    print(f"{label} shared setup implementation copied into {shared_target}")

    print(f"{label} the generated copies are tracked: commit the result")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
