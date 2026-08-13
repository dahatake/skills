"""Location-independent launcher for the vendored Code Query GUI."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    here = Path(__file__).resolve().parent
    vendor = here / "vendor"
    if not (vendor / "cq" / "gui" / "__main__.py").is_file():
        print(
            "[code-query gui] vendor/cq is missing or has no GUI. "
            "The kit was copied without its engine; re-copy the whole directory.",
            file=sys.stderr,
        )
        return 2
    if str(vendor) not in sys.path:
        sys.path.insert(0, str(vendor))

    try:
        from cq.gui.__main__ import main as gui_main
    except ImportError as exc:
        print(f"[code-query gui] failed to import GUI: {exc}", file=sys.stderr)
        return 2
    return gui_main()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
