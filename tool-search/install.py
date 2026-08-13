#!/usr/bin/env python3
"""コピー先リポジトリでのセットアップ判断（版管理付き配布キット共通）。

`install.ps1` / `install.sh` は「Python と git が無ければ OS のパッケージ
マネージャで入れる」ところまでしか行わない。ここから先の判断（venv 作成・
依存インストール・設定生成・Skill 配置・初回索引）は上流の
`kit/kit_setup.py` が単一実装として持っているので、本ファイルは
`kit.toml` を読んで適切な引数を組み立てて委譲するだけにする。

直接実行する場合（Python 導入済みの環境）::

    python install.py --repo-root .
    python install.py --repo-root . --with-gui --with-watch --force
    python install.py --version      # 導入済みの版を見る
    python install.py --verify       # 同梱ファイルの改変・欠落を見る
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Sequence

KIT_DIR = Path(__file__).resolve().parent
MANIFEST_NAME = "KIT-VERSION.json"
EXTRAS_NAME = "install-extras.json"
MIN_PYTHON = (3, 11)


def load_kit(kit_dir: Path) -> dict:
    path = kit_dir / "kit.toml"
    if not path.is_file():
        raise SystemExit(
            f"kit.toml が見つかりません: {path}\n"
            "配布フォルダが不完全です。copy_to_repo.py でコピーし直してください。"
        )
    return tomllib.loads(path.read_text(encoding="utf-8"))["kit"]


def kit_version(kit_dir: Path) -> str:
    data = _read_manifest(kit_dir)
    return f"{data.get('version', '?')} (engine {data.get('engine_version', '?')})"


def post_install_note(kit_dir: Path) -> str:
    return str(_read_manifest(kit_dir).get("post_install_note", ""))


def _read_manifest(kit_dir: Path) -> dict:
    path = kit_dir / MANIFEST_NAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def extra_dependencies(kit_dir: Path) -> list[str]:
    path = kit_dir / EXTRAS_NAME
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"{path} を読めません: {exc}")
    return [str(item) for item in data.get("packages", [])]


def show_version(kit_dir: Path) -> int:
    data = _read_manifest(kit_dir)
    if not data:
        print(f"{MANIFEST_NAME} がありません: {kit_dir}", file=sys.stderr)
        return 1
    for key in (
        "package", "version", "engine", "engine_version",
        "source_repo", "source_commit", "copied_at", "file_count",
    ):
        print(f"{key:<15}: {data.get(key)}")
    return 0


def verify(kit_dir: Path) -> int:
    """同梱ファイルが配布時の内容のままかを見る。上流リポジトリを参照しない。"""
    manifest = _read_manifest(kit_dir)
    recorded = manifest.get("files")
    if not isinstance(recorded, dict) or not recorded:
        print(f"{MANIFEST_NAME} にハッシュがありません: {kit_dir}", file=sys.stderr)
        return 1
    preserved = set(manifest.get("preserved") or ())
    drift: list[str] = []
    for rel, digest in recorded.items():
        if rel in preserved:
            continue
        target = kit_dir / rel
        if not target.is_file():
            drift.append(f"{rel} (欠落)")
        elif hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            drift.append(f"{rel} (改変)")
    print(
        f"同梱ファイル {len(recorded)} 件 / 温存 {len(preserved)} 件 / "
        f"改変・欠落 {len(drift)} 件"
    )
    for rel in drift:
        print(f"    - {rel}")
    return 1 if drift else 0


def venv_python(venv: Path) -> Path:
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def install_extras(
    kit_dir: Path, manifest: dict, bootstrap: str, packages: list[str], label: str
) -> None:
    """kit_setup が使う venv を先に用意し、追加依存を入れておく。

    kit_setup は venv が既にあれば作り直さないので、ここで作っておけば
    base_dependencies のインストールと初回索引はそのまま委譲できる。
    """
    venv = kit_dir / manifest["venv"]
    interpreter = venv_python(venv)
    if not interpreter.is_file():
        print(f"{label} creating venv at {venv} ...")
        subprocess.run([bootstrap, "-m", "venv", str(venv)], check=True)
    print(f"{label} installing extras: {', '.join(packages)}")
    subprocess.run([str(interpreter), "-m", "pip", "install", *packages], check=True)


def main(argv: Sequence[str] | None = None) -> int:
    # Do not force an encoding: on Windows Python already writes in the console's
    # own code page, and install.ps1 switches both sides to UTF-8 together. Only
    # make an unencodable character survive instead of aborting the run.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):
            pass

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit-dir", type=Path, default=KIT_DIR)
    parser.add_argument(
        "--repo-root", type=Path, default=Path.cwd(),
        help="導入先リポジトリのルート（既定: カレントディレクトリ）",
    )
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--profile", default=None)
    parser.add_argument("--with-gui", action="store_true")
    parser.add_argument("--with-watch", action="store_true")
    parser.add_argument("--with-tokenizer", action="store_true")
    parser.add_argument("--no-index", action="store_true", help="初回索引をスキップする")
    parser.add_argument("--no-skill", action="store_true", help="Skill 定義の配置をスキップする")
    parser.add_argument(
        "--no-extras", action="store_true",
        help="install-extras.json の追加依存を導入しない（wheel が無い環境向けの退避策）",
    )
    parser.add_argument("--no-venv", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--version", action="store_true", help="導入済みの版を表示して終了する"
    )
    parser.add_argument(
        "--verify", action="store_true", help="同梱ファイルの改変・欠落を調べて終了する"
    )
    args = parser.parse_args(argv)

    if args.version:
        return show_version(args.kit_dir.resolve())
    if args.verify:
        return verify(args.kit_dir.resolve())

    if sys.version_info < MIN_PYTHON:
        raise SystemExit(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 以上が必要です "
            f"(現在: {sys.version.split()[0]})"
        )

    kit_dir = args.kit_dir.resolve()
    repo_root = args.repo_root.resolve()
    manifest = load_kit(kit_dir)
    entry = kit_dir / "kit" / "kit_setup.py"
    if not entry.is_file():
        raise SystemExit(f"共通セットアップ実装が見つかりません: {entry}")

    label = f"[{manifest['skill']} install]"
    print(f"{label} kit  : {kit_dir} v{kit_version(kit_dir)}")
    print(f"{label} repo : {repo_root}")
    print(f"{label} python: {args.python}")

    extras = [] if (args.no_extras or args.no_venv) else extra_dependencies(kit_dir)
    if extras:
        install_extras(kit_dir, manifest, str(args.python), extras, label)

    argv_out = [
        str(args.python), str(entry),
        "--kit-dir", str(kit_dir),
        "--repo-root", str(repo_root),
        "--python", str(args.python),
    ]
    if args.profile:
        argv_out += ["--profile", args.profile]
    for flag, enabled in (
        ("--with-gui", args.with_gui),
        ("--with-watch", args.with_watch),
        ("--with-tokenizer", args.with_tokenizer),
        ("--no-venv", args.no_venv),
        ("--force", args.force),
    ):
        if enabled:
            argv_out.append(flag)

    # Skill 定義を同梱していないキット（tool-search）では配置しない。
    if not args.no_skill and (kit_dir / "skill" / "SKILL.md").is_file():
        argv_out.append("--install-skill")
    # 索引を持たないキットでは索引構築コマンド自体が無い。
    if not args.no_index and bool(manifest.get("supports_index", True)):
        argv_out.append("--build-index")

    code = subprocess.run(argv_out, cwd=str(repo_root)).returncode

    note = post_install_note(kit_dir)
    if code == 0 and note:
        print(f"\n{label} 次にやること:\n{note}")
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
