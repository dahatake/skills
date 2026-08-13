"""cq configuration and profile resolution (FR-CQ-01 / FR-CQ-03).

Unlike `mdq`, `cq` has no implicit default roots: an unconfigured repository is
an error rather than a guess, because indexing the wrong tree silently produces
confident but wrong answers.

Resolution order (first hit wins):

1. explicit path passed by the caller
2. ``<repo_root>/cq.toml``
3. ``<repo_root>/.cq/config.toml``

Schema (TOML)::

    [index]
    max_file_bytes = 2097152

    [profiles.hve]
    roots = ["hve", "mdq", "cq"]
    exclude = ["hve/gui/i18n/**"]
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Optional

CONFIG_FILENAMES: tuple[Path, ...] = (Path("cq.toml"), Path(".cq") / "config.toml")

DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024

# 一次的な除外機構は discovery.LANGUAGE_BY_SUFFIX の拡張子 allowlist。
# ここには allowlist を通ってしまう拡張子（.js / .py 等）を持つ複製・生成物と、
# 万一 allowlist が拡張されても資格情報を読まないための防御的パターンだけを置く。
BUILTIN_EXCLUDES: tuple[str, ...] = (
    "**/vendor/**",
    "**/node_modules/**",
    "*.min.js",
    "**/.env",
    "**/.env.*",
    "*.pem",
    "*.key",
    "*.pfx",
    "*.p12",
    "**/id_rsa*",
)


class ConfigError(ValueError):
    """Raised when the configuration is missing or cannot be trusted."""


@dataclass(frozen=True)
class Profile:
    name: str
    roots: tuple[str, ...]
    exclude: tuple[str, ...]
    max_file_bytes: int


def find_config(repo_root: Path) -> Optional[Path]:
    for rel in CONFIG_FILENAMES:
        candidate = repo_root / rel
        if candidate.is_file():
            return candidate
    return None


def load_config(repo_root: Path, explicit: Optional[Path] = None) -> dict[str, Any]:
    path = explicit if explicit is not None else find_config(repo_root)
    if path is None or not path.is_file():
        raise ConfigError(
            f"no cq configuration found under {repo_root}: "
            f"declare profiles in one of {', '.join(str(p) for p in CONFIG_FILENAMES)}"
        )
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read cq configuration {path}: {exc}") from exc


def _normalise_root(value: Any, profile: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"profile {profile!r}: every root must be a non-empty string")
    raw = value.strip()
    if "\\" in raw or raw.startswith("/") or "//" in raw:
        raise ConfigError(f"profile {profile!r}: invalid root {value!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ConfigError(f"profile {profile!r}: invalid root {value!r}")
    return f"{path}/"


def resolve_profiles(
    repo_root: Path, explicit: Optional[Path] = None
) -> dict[str, Profile]:
    raw = load_config(repo_root, explicit)
    declared = raw.get("profiles")
    if not isinstance(declared, dict) or not declared:
        raise ConfigError("cq configuration declares no [profiles.<name>] section")

    index = raw.get("index") if isinstance(raw.get("index"), dict) else {}
    max_bytes = index.get("max_file_bytes", DEFAULT_MAX_FILE_BYTES)
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ConfigError("[index].max_file_bytes must be a positive integer")

    profiles: dict[str, Profile] = {}
    for name, body in declared.items():
        if not isinstance(body, dict):
            raise ConfigError(f"profile {name!r}: must be a table")
        roots = body.get("roots")
        if not isinstance(roots, list) or not roots:
            raise ConfigError(f"profile {name!r}: 'roots' must be a non-empty list")
        extra = body.get("exclude", [])
        if not isinstance(extra, list) or any(not isinstance(e, str) for e in extra):
            raise ConfigError(f"profile {name!r}: 'exclude' must be a list of strings")
        profiles[name] = Profile(
            name=name,
            roots=tuple(_normalise_root(r, name) for r in roots),
            exclude=BUILTIN_EXCLUDES + tuple(extra),
            max_file_bytes=max_bytes,
        )
    return profiles


def resolve_profile(
    repo_root: Path, name: str, explicit: Optional[Path] = None
) -> Profile:
    profiles = resolve_profiles(repo_root, explicit)
    try:
        return profiles[name]
    except KeyError:
        raise ConfigError(
            f"unknown profile {name!r}; declared profiles: {', '.join(sorted(profiles))}"
        ) from None
