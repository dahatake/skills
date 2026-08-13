"""capture_screenshots.py — Auto-generate screenshots of the 3 tabs.

Runs against an offscreen Qt platform (``QT_QPA_PLATFORM=offscreen``) so it
works on headless CI/dev machines. Output:

    docs/images/screenshot-basic.png
    docs/images/screenshot-index.png
    docs/images/screenshot-stats.png

Usage:
    python tools/capture_screenshots.py

Override repo_root for the section's stats lookup with --repo-root <path>.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "docs" / "images",
    )
    args = parser.parse_args()

    # Force offscreen platform for headless capture.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    # Make the standalone Skill imports work regardless of how the package
    # was copied (no dependency on the ``tools.skills.markdown_query`` path).
    # File path: <skill_dir>/tools/capture_screenshots.py
    skill_dir = Path(__file__).resolve().parent.parent
    if str(skill_dir) not in sys.path:
        sys.path.insert(0, str(skill_dir))
    vendor_dir = skill_dir / "vendor"
    if vendor_dir.is_dir() and str(vendor_dir) not in sys.path:
        sys.path.insert(0, str(vendor_dir))

    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFont, QFontDatabase

    from mdq.gui.settings_section import MdqIndexSection

    app = QApplication.instance() or QApplication(sys.argv)

    # Qt offscreen does not ship fonts. On Windows / macOS / common Linux
    # distributions there is usually a system Japanese font we can register
    # via addApplicationFont() so that CJK glyphs render correctly.
    font_candidates = []
    if sys.platform == "win32":
        win_fonts = Path(r"C:\Windows\Fonts")
        font_candidates += [
            win_fonts / "YuGothM.ttc",
            win_fonts / "YuGothR.ttc",
            win_fonts / "meiryo.ttc",
            win_fonts / "msgothic.ttc",
            win_fonts / "msmincho.ttc",
        ]
    elif sys.platform == "darwin":
        font_candidates += [
            Path("/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc"),
            Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
        ]
    else:
        font_candidates += [
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/fonts-japanese-gothic.ttf"),
        ]

    chosen_family: str | None = None
    for fp in font_candidates:
        if fp.exists():
            fid = QFontDatabase.addApplicationFont(str(fp))
            if fid >= 0:
                fams = QFontDatabase.applicationFontFamilies(fid)
                if fams:
                    chosen_family = fams[0]
                    break

    if chosen_family is not None:
        app.setFont(QFont(chosen_family, 9))
        print(f"[capture] using font: {chosen_family}", file=sys.stderr)
    else:
        print(
            "[capture] WARN: no CJK font found; screenshots will show tofu boxes.",
            file=sys.stderr,
        )

    # Use the Skill directory as repo_root by default so that index stats /
    # usage-report lookups have a writable location.
    if args.repo_root is not None:
        repo_root = args.repo_root.resolve()
        cleanup_tmp = None
    else:
        repo_root = skill_dir
        cleanup_tmp = None

    args.out_dir.mkdir(parents=True, exist_ok=True)

    section = MdqIndexSection(repo_root=repo_root)
    section.resize(900, 700)
    section.show()
    app.processEvents()

    titles = ["basic", "index", "stats"]
    for i, name in enumerate(titles):
        section._tabs.setCurrentIndex(i)
        app.processEvents()
        pix = section.grab()
        out = args.out_dir / f"screenshot-{name}.png"
        ok = pix.save(str(out), "PNG")
        if not ok:
            print(f"failed to save {out}", file=sys.stderr)
            return 1
        print(f"wrote {out}")

    section.close()
    if cleanup_tmp is not None:
        cleanup_tmp.cleanup()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
