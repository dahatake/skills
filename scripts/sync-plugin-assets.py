#!/usr/bin/env python3
"""Regenerate the generated plugin assets from their source of truth.

Two mirrors exist because the plugin hosts require a fixed layout:

* `skills/<kit>/`            <- `<kit>/skill/`   (declared by `plugin.json`)
* `.claude-plugin/plugin.json` <- `plugin.json`  (Claude Code reads its own copy)

Both are byte-for-byte copies, and `.gitattributes` marks them `-text` so the
bytes survive a checkout on any platform.

    python scripts/sync-plugin-assets.py            # write the mirrors
    python scripts/sync-plugin-assets.py --check    # fail if out of sync
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / "skills"
PLUGIN_JSON = REPO_ROOT / "plugin.json"
CLAUDE_PLUGIN_JSON = REPO_ROOT / ".claude-plugin" / "plugin.json"

README_BYTES = """# skills/ (generated)

このディレクトリは `scripts/sync-plugin-assets.py` が各キットの `skill/` から
生成します。**直接編集しないでください。**

- 正本: `markdown-query/skill/`, `code-query/skill/`
- 用途: `plugin.json` / `.claude-plugin/marketplace.json` が参照するプラグイン配布物

SKILL.md だけでは動作しません。CLI 本体は各キットの `install.ps1` / `install.sh`
で導入してください。詳細はリポジトリルートの `README.md` を参照してください。
""".encode("utf-8")


def kits_with_skill() -> list[Path]:
    return sorted(
        manifest.parent
        for manifest in REPO_ROOT.glob("*/kit.toml")
        if (manifest.parent / "skill" / "SKILL.md").is_file()
    )


def tree_bytes(root: Path) -> dict[str, bytes]:
    if not root.is_dir():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def compare(src: dict[str, bytes], dst: dict[str, bytes], label: str) -> list[str]:
    drift = [f"{label}/{rel} (missing or modified)" for rel, data in src.items() if dst.get(rel) != data]
    drift += [f"{label}/{rel} (stale, not in the kit)" for rel in dst if rel not in src]
    return drift


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify without writing")
    args = parser.parse_args(argv)

    kits = kits_with_skill()
    if not kits:
        print("no kit ships a skill/ directory", file=sys.stderr)
        return 2

    snapshots = {
        kit: (tree_bytes(kit / "skill"), tree_bytes(SKILLS_DIR / kit.name)) for kit in kits
    }
    drift: list[str] = []
    for kit, (src, dst) in snapshots.items():
        drift += compare(src, dst, kit.name)

    expected = {kit.name for kit in kits}
    present = {p.name for p in SKILLS_DIR.iterdir() if p.is_dir()} if SKILLS_DIR.is_dir() else set()
    for stale in sorted(present - expected):
        drift.append(f"{stale}/ (stale kit directory)")

    readme = SKILLS_DIR / "README.md"
    if not readme.is_file() or readme.read_bytes() != README_BYTES:
        drift.append("README.md (missing or modified)")

    if not CLAUDE_PLUGIN_JSON.is_file() or CLAUDE_PLUGIN_JSON.read_bytes() != PLUGIN_JSON.read_bytes():
        drift.append(".claude-plugin/plugin.json (out of sync with plugin.json)")

    if args.check:
        if drift:
            print("generated plugin assets are out of sync:", file=sys.stderr)
            for item in drift:
                print(f"    {item}", file=sys.stderr)
            print("run: python scripts/sync-plugin-assets.py", file=sys.stderr)
            return 1
        print(f"plugin assets are in sync with {len(kits)} kit(s)")
        return 0

    for kit, (src, dst) in snapshots.items():
        dest = SKILLS_DIR / kit.name
        for removed in sorted(set(dst) - set(src)):
            print(f"removing {kit.name}/{removed} (no longer shipped by the kit)")
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(kit / "skill", dest)
        print(f"wrote skills/{kit.name}/")

    for stale in sorted(present - expected):
        shutil.rmtree(SKILLS_DIR / stale)
        print(f"removed stale skills/{stale}/")

    readme.write_bytes(README_BYTES)
    print("wrote skills/README.md")

    CLAUDE_PLUGIN_JSON.write_bytes(PLUGIN_JSON.read_bytes())
    print("wrote .claude-plugin/plugin.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
