"""File discovery and exclusion for cq (FR-CQ-03).

Enumeration always starts from git's view of the working tree, so `.gitignore`
is honoured without cq re-implementing ignore semantics. Anything that cannot be
judged confidently is skipped rather than indexed.
"""

from __future__ import annotations

import fnmatch
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Sequence

from cq.config import Profile
from cq.languages import resolve_language

GIT_LIST_COMMAND = ("git", "ls-files", "--cached", "--others", "--exclude-standard")


class DiscoveryError(RuntimeError):
    """Raised when the working tree cannot be enumerated."""


# 実装をテストより上位に出すための判定。検索と俯瞰マップで共有する（二重定義の禁止）。
_TEST_PATH_GLOBS = (
    "*/tests/*", "tests/*", "*/test/*", "test/*",
    "*/test_*", "test_*", "*.Tests/*", "*_test.*", "*.test.*", "*.spec.*",
)


def test_path_sql(column: str) -> str:
    """SQL expression yielding 1 for test code and 0 otherwise."""
    conditions = " OR ".join(f"{column} GLOB '{glob}'" for glob in _TEST_PATH_GLOBS)
    return f"CASE WHEN {conditions} THEN 1 ELSE 0 END"


@dataclass(frozen=True)
class DiscoveredFile:
    path: str
    lang: str
    size_bytes: int
    mtime: float


def git_lister(repo_root: Path) -> tuple[str, ...]:
    try:
        out = subprocess.check_output(
            list(GIT_LIST_COMMAND), cwd=repo_root, text=True, encoding="utf-8",
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise DiscoveryError("git is required to enumerate files for cq") from exc
    except subprocess.CalledProcessError as exc:
        raise DiscoveryError(
            f"cannot enumerate files under {repo_root}: {(exc.stderr or '').strip()}"
        ) from exc
    return tuple(out.splitlines())


def _is_safe(rel: str) -> bool:
    if not rel or "\\" in rel or rel.startswith("/") or "//" in rel:
        return False
    path = PurePosixPath(rel)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def is_excluded(rel: str, patterns: Sequence[str]) -> bool:
    """Case-sensitive glob match so that indexing is identical on every platform."""
    parts = PurePosixPath(rel).parts
    name = parts[-1] if parts else rel
    for pattern in patterns:
        if fnmatch.fnmatchcase(rel, pattern) or fnmatch.fnmatchcase(name, pattern):
            return True
        if pattern.startswith("**/"):
            tail = pattern[3:]
            # `**/` は「0 個以上の先行ディレクトリ」。fnmatch 単体では 0 個の場合に一致しない。
            if fnmatch.fnmatchcase(rel, tail):
                return True
            if tail.endswith("/**"):
                segment = tail[:-3]
                if segment and segment in parts[:-1]:
                    return True
    return False


def iter_files(
    repo_root: Path,
    profile: Profile,
    *,
    lister: Callable[[Path], Sequence[str]] = git_lister,
) -> tuple[DiscoveredFile, ...]:
    """Return the indexable files of ``profile``, sorted by path for determinism."""
    found: list[DiscoveredFile] = []
    for rel in lister(repo_root):
        if not _is_safe(rel) or not rel.startswith(tuple(profile.roots)):
            continue
        absolute = repo_root / rel
        lang = resolve_language(
            PurePosixPath(rel).suffix.lower(),
            lambda p=absolute: p.read_text(encoding="utf-8", errors="replace"),
        )
        if lang is None or is_excluded(rel, profile.exclude):
            continue
        try:
            stat = absolute.stat()
        except OSError:
            continue  # 判定できないものは索引しない（fail-closed）
        if stat.st_size > profile.max_file_bytes:
            continue
        found.append(DiscoveredFile(rel, lang, stat.st_size, stat.st_mtime))
    found.sort(key=lambda f: f.path)
    return tuple(found)
