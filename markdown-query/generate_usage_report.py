#!/usr/bin/env python3
"""Thin CLI wrapper for :mod:`mdq.usage_report`.

The report generator itself lives in the engine package so that the GUI and
the CLI share a single implementation (FR-KIT-03). This wrapper only keeps
the documented invocation path working.

The engine derives its default repository root from its own file location. When
the engine is imported from ``vendor/`` that resolves to the vendor directory,
so the report would aggregate a usage log that does not exist there. The
wrapper therefore resolves the repository root from the kit's own position and
passes it explicitly.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_VENDOR = _HERE / "vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

from mdq.usage_report import generate_report, main as _engine_main  # noqa: E402,F401


def default_repo_root() -> Path:
    """Repository that owns ``.mdq/usage.jsonl``.

    The kit can be dropped anywhere inside the target repository, so the root is
    found by walking up rather than by assuming a fixed nesting depth. The walk
    stops at the repository boundary so a stray ``.mdq`` further up the
    filesystem cannot silently redirect the report to another project.
    """
    for candidate in (_HERE, *_HERE.parents):
        if (candidate / "mdq.toml").is_file() or (candidate / ".mdq").is_dir():
            return candidate
        if (candidate / ".git").exists():
            break
    return _HERE


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not any(a == "--repo-root" or a.startswith("--repo-root=") for a in args):
        args += ["--repo-root", str(default_repo_root())]
    return _engine_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
