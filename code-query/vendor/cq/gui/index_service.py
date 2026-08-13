"""Code Query index operations shared by standalone and HVE GUIs."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from cq import config as cq_config
from cq import discovery as cq_discovery
from cq import indexer as cq_indexer
from cq import search as cq_search
from cq import store as cq_store

_NOT_BUILT = "未作成"
_OPERATIONAL_ERRORS = (
    cq_config.ConfigError,
    cq_store.StoreError,
    cq_discovery.DiscoveryError,
    OSError,
)


def config_candidates() -> list[str]:
    return [rel.as_posix() for rel in cq_config.CONFIG_FILENAMES]


def list_profiles(repo_root: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "profiles": {},
        "config_path": None,
        "config_candidates": config_candidates(),
        "error": None,
    }
    try:
        profiles = cq_config.resolve_profiles(repo_root)
    except cq_config.ConfigError as exc:
        result["error"] = str(exc)
        return result

    found = cq_config.find_config(repo_root)
    result["config_path"] = str(found) if found is not None else None
    result["profiles"] = {
        name: {
            "roots": profile.roots,
            "exclude": profile.exclude,
            "max_file_bytes": profile.max_file_bytes,
        }
        for name, profile in profiles.items()
    }
    return result


def _resolve(
    repo_root: Path, profile_name: str
) -> tuple[cq_config.Profile | None, str | None]:
    try:
        return cq_config.resolve_profile(repo_root, profile_name), None
    except cq_config.ConfigError as exc:
        return None, str(exc)


def _db_path(repo_root: Path, profile_name: str) -> tuple[Path | None, str | None]:
    try:
        return (repo_root / cq_store.db_path_for(profile_name)).resolve(), None
    except cq_store.StoreError as exc:
        return None, str(exc)


def _mtime_iso(path: Path) -> str:
    if not path.exists():
        return _NOT_BUILT
    return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")


def _empty_stats(profile_name: str, db_path: Path | None, error: str | None) -> dict:
    stats: dict[str, Any] = {
        "profile": profile_name,
        "db_path": str(db_path) if db_path is not None else "",
        "db_exists": False,
        "db_mtime": _NOT_BUILT,
        "schema_version": None,
        "by_parser": {},
        "by_lang": {},
        "error": error,
    }
    stats.update({table: 0 for table in cq_store.STATS_TABLES})
    return stats


def get_index_stats(repo_root: Path, profile_name: str) -> dict[str, Any]:
    profile, error = _resolve(repo_root, profile_name)
    db_path, path_error = _db_path(repo_root, profile_name)
    error = error or path_error
    if profile is None or db_path is None or not db_path.exists():
        return _empty_stats(profile_name, db_path, error)

    try:
        raw = cq_store.index_stats(db_path)
    except cq_store.StoreError as exc:
        return _empty_stats(profile_name, db_path, str(exc))

    stats = dict(raw)
    stats.pop("db", None)
    stats.update({
        "profile": profile_name,
        "db_path": str(db_path),
        "db_exists": True,
        "db_mtime": _mtime_iso(db_path),
        "error": None,
    })
    return stats


def get_index_stats_all_profiles(repo_root: Path) -> dict[str, dict[str, Any]]:
    listing = list_profiles(repo_root)
    return {
        name: get_index_stats(repo_root, name)
        for name in listing["profiles"]
    }


def build(
    repo_root: Path, profile_name: str, *, rebuild: bool = False
) -> dict[str, Any]:
    empty = cq_indexer.IndexReport().to_dict()
    profile, error = _resolve(repo_root, profile_name)
    db_path, path_error = _db_path(repo_root, profile_name)
    error = error or path_error
    if profile is None or db_path is None:
        return {**empty, "elapsed_ms": 0, "error": error}

    started = perf_counter()
    try:
        report = cq_indexer.build_index(
            repo_root, profile, db_path=db_path, rebuild=rebuild
        )
    except _OPERATIONAL_ERRORS as exc:
        return {**empty, "elapsed_ms": _elapsed_ms(started), "error": str(exc)}
    return {**report.to_dict(), "elapsed_ms": _elapsed_ms(started), "error": None}


def _elapsed_ms(started: float) -> int:
    return int((perf_counter() - started) * 1000)


def delete_index_db(repo_root: Path, profile_name: str) -> list[str]:
    db_path, _ = _db_path(repo_root, profile_name)
    if db_path is None:
        return []
    removed: list[str] = []
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(db_path) + suffix)
        if candidate.exists():
            candidate.unlink()
            removed.append(str(candidate))
    return removed


def search_preview(
    repo_root: Path,
    profile_name: str,
    query: str,
    *,
    mode: str = "auto",
    top_k: int = 5,
) -> dict[str, Any]:
    db_path, path_error = _db_path(repo_root, profile_name)
    if db_path is None:
        return {"hits": [], "staleness": None, "error": path_error}
    try:
        hits = cq_search.search(
            repo_root,
            profile_name,
            query=query,
            mode=mode,
            top_k=top_k,
            db_path=db_path,
        )
    except (cq_search.SearchError, cq_store.StoreError) as exc:
        return {"hits": [], "staleness": None, "error": str(exc)}
    return {
        "hits": [hit.to_dict() for hit in hits],
        "staleness": cq_search.last_staleness(),
        "error": None,
    }
