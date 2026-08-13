"""Entry point for the standalone Code Query management window."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

__version__ = "0.2.0"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="code-query-gui",
        description="Standalone management GUI for the Code Query Skill.",
    )
    parser.add_argument(
        "repo_root",
        nargs="?",
        default=None,
        help="Repository root to manage (default: current working directory).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"code-query-gui {__version__}",
    )
    args = parser.parse_args(argv)

    repo_root = (
        Path(args.repo_root).resolve()
        if args.repo_root
        else Path.cwd().resolve()
    )
    if not repo_root.is_dir():
        print(
            f"[code-query gui] repo_root is not a directory: {repo_root}",
            file=sys.stderr,
        )
        return 2

    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print(
            "[code-query gui] PySide6 is not installed.\n"
            "  Run setup.ps1 -WithGui (Windows) or setup.sh --with-gui "
            "(Linux/macOS).",
            file=sys.stderr,
        )
        return 2

    from .standalone_window import StandaloneWindow

    application = QApplication.instance() or QApplication(sys.argv[:1])
    window = StandaloneWindow(repo_root=repo_root)
    window.show()
    return application.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
