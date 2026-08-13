"""Shared setup logic for the Skill distribution kits (FR-KIT-03).

Every kit ships a copy of this directory as ``<kit>/kit/``. The per-OS launchers
(`setup.ps1` / `setup.sh`) only resolve a bootstrap interpreter and delegate
here, so dependency resolution, path decisions, configuration scaffolding and
Skill placement exist in one place.

Run from the kit directory (the launchers do this for you)::

    python kit/kit_setup.py --kit-dir . --repo-root . --install-skill --build-index
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Sequence


def load_manifest(kit_dir: Path) -> dict:
    path = kit_dir / "kit.toml"
    if not path.is_file():
        raise SystemExit(f"kit manifest not found: {path}")
    return tomllib.loads(path.read_text(encoding="utf-8"))["kit"]


def vendor_dir(kit_dir: Path) -> Path:
    return kit_dir / "vendor"


def verify_vendor(kit_dir: Path, engine: str, label: str) -> Path:
    path = vendor_dir(kit_dir) / engine / "cli.py"
    if not path.is_file():
        raise SystemExit(
            f"{label} vendor/{engine} is missing or incomplete ({path}). "
            "The kit was copied without its engine; re-copy the whole directory."
        )
    return vendor_dir(kit_dir)


def venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def ensure_venv(bootstrap: str, venv: Path, label: str) -> Path:
    interpreter = venv_python(venv)
    if not interpreter.is_file():
        print(f"{label} creating venv at {venv} ...")
        subprocess.run([bootstrap, "-m", "venv", str(venv)], check=True)
    return interpreter


def install(interpreter: Path, packages: Sequence[str], label: str) -> None:
    if not packages:
        return
    print(f"{label} installing: {', '.join(packages)}")
    subprocess.run(
        [str(interpreter), "-m", "pip", "install", "--upgrade", "pip"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    subprocess.run([str(interpreter), "-m", "pip", "install", *packages], check=True)


def generate_config(
    kit_dir: Path,
    interpreter: Path,
    manifest: dict,
    repo_root: Path,
    profile: str | None,
    force: bool,
    label: str,
) -> None:
    scaffolder = kit_dir / manifest.get("init_config", "init_config.py")
    config = repo_root / manifest["config"]
    if not scaffolder.is_file():
        return
    if config.is_file() and not force:
        print(f"{label} {config} already exists; keeping it (use --force to regenerate).")
        return
    argv = [str(interpreter), str(scaffolder), "--repo-root", str(repo_root)]
    if profile and manifest.get("config_supports_profile", False):
        argv += ["--profile", profile]
    if force:
        argv.append("--force")
    completed = subprocess.run(argv, env=_env_with_vendor(kit_dir))
    if completed.returncode != 0:
        # The scaffolder already explained itself on stderr; a traceback here
        # would only bury that message.
        raise SystemExit(
            f"{label} could not generate {config}. Fix the reason printed above "
            "and re-run the installer."
        )


def install_skill(
    kit_dir: Path, repo_root: Path, skill: str, force: bool, label: str
) -> None:
    source = kit_dir / "skill"
    if not source.is_dir():
        raise SystemExit(
            f"{label} skill definition not found: {source} (run sync-vendor first)"
        )
    target = repo_root / ".github" / "skills" / skill
    if (target / "SKILL.md").is_file() and not force:
        print(f"{label} {target} already exists; keeping it (use --force to overwrite).")
        return
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    print(f"{label} wrote {target}")


def _env_with_vendor(kit_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    vendor = str(vendor_dir(kit_dir))
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{vendor}{os.pathsep}{existing}" if existing else vendor
    return env


def build_index(
    kit_dir: Path,
    interpreter: Path,
    engine: str,
    repo_root: Path,
    profile: str | None,
    manifest: dict,
    label: str,
) -> None:
    print(f"{label} building the initial index in {repo_root} ...")
    argv = [str(interpreter), "-m", engine, "index"]
    if profile and manifest.get("config_supports_profile", False):
        argv += ["--profile", profile]
    result = subprocess.run(
        argv, cwd=str(repo_root), env=_env_with_vendor(kit_dir)
    )
    if result.returncode != 0:
        print(f"{label} WARN: initial index build failed.", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--python", default=sys.executable,
                        help="Bootstrap interpreter used to create the venv.")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--with-gui", action="store_true")
    parser.add_argument("--with-watch", action="store_true")
    parser.add_argument("--with-tokenizer", action="store_true")
    parser.add_argument("--install-skill", action="store_true")
    parser.add_argument("--build-index", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-venv", action="store_true",
                        help="Use the bootstrap interpreter and skip dependency install.")
    args = parser.parse_args(argv)

    kit_dir = args.kit_dir.resolve()
    repo_root = args.repo_root.resolve()
    manifest = load_manifest(kit_dir)
    engine = manifest["engine"]
    skill = manifest["skill"]
    label = f"[{skill} setup]"

    verify_vendor(kit_dir, engine, label)

    if args.no_venv:
        interpreter = Path(args.python)
    else:
        interpreter = ensure_venv(args.python, kit_dir / manifest["venv"], label)
        packages: list[str] = list(manifest.get("base_dependencies", []))
        if args.with_gui:
            packages += manifest.get("gui_dependencies", [])
        if args.with_watch:
            packages += manifest.get("watch_dependencies", [])
        if args.with_tokenizer:
            packages += manifest.get("tokenizer_dependencies", [])
        install(interpreter, packages, label)

    profile = args.profile or manifest.get("default_profile")
    generate_config(kit_dir, interpreter, manifest, repo_root, profile, args.force, label)

    if args.install_skill:
        install_skill(kit_dir, repo_root, skill, args.force, label)

    if args.build_index:
        build_index(kit_dir, interpreter, engine, repo_root, profile, manifest, label)

    print(f"{label} done.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
