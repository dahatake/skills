"""Index operation service shared by the standalone and HVE Markdown-Query GUIs.

This module is the single implementation (FR-GUI-05). ``hve.gui.mdq_index_service``
re-exports these functions and must not carry a second implementation.
"""

from __future__ import annotations

from datetime import datetime
from time import perf_counter
from pathlib import Path
from typing import Any, Iterable, List, Optional, Protocol

from mdq import cli as mdq_cli
from mdq import indexer as mdq_indexer
from mdq import store as mdq_store
from mdq.strategies import ALL_STRATEGIES

from . import settings_store


class SettingsBackend(Protocol):
    """Persistence seam between the standalone and HVE hosts.

    This is the single definition of the seam; the management panel imports it
    instead of declaring its own, so a host cannot satisfy the panel while
    failing the index service at run time.
    """

    def load(self, repo_root: Path) -> dict: ...
    def save(self, repo_root: Path, settings: dict) -> None: ...
    def get_mdq_target_folders(self, repo_root: Path) -> List[str]: ...


def _resolve_db_path(
    repo_root: Path,
    db_path: Path | None = None,
    *,
    lang: str = "ja-jp",
    strategy: str = "heading",
) -> Path:
    if db_path is not None:
        return db_path
    return (repo_root / mdq_store.db_path_for(lang, strategy)).resolve()


def index_artifact_path(
    repo_root: Path,
    *,
    lang: str = "ja-jp",
    strategy: str = "heading",
) -> Path:
    """Return where ``strategy`` materialises its index under ``repo_root``.

    ``graphrag`` owns a LightRAG working directory instead of a SQLite file,
    so callers must not assume the SQLite layout for every strategy.
    """
    if strategy == "graphrag":
        return (repo_root / mdq_store.graphrag_dir_for(lang)).resolve()
    return _resolve_db_path(repo_root, None, lang=lang, strategy=strategy)


def _index_exists(artifact: Path, strategy: str) -> bool:
    """Return True when ``artifact`` is a built index.

    For ``graphrag`` an existing but empty working directory does not count:
    the directory is created before LightRAG runs, so a failed build leaves
    one behind.
    """
    if strategy == "graphrag":
        return mdq_indexer.has_lightrag_index(artifact)
    return artifact.exists()


def _file_mtime_iso(path: Path) -> str:
    if not path.exists():
        return "未作成"
    ts = datetime.fromtimestamp(path.stat().st_mtime)
    return ts.isoformat(timespec="seconds")


def _missing_index_stats(
    index_path: Path, *, lang: str, strategy: str
) -> dict:
    """Stats payload for a strategy whose index has not been built yet.

    Reporting statistics must never materialise an index: ``open_store``
    creates the SQLite file as a side effect, which would make an unbuilt
    strategy indistinguishable from a built-but-empty one.
    """
    return {
        "db_path": str(index_path),
        "db_exists": False,
        "db_mtime": "未作成",
        "schema_version": "-" if strategy == "graphrag" else mdq_store.SCHEMA_VERSION,
        "fts5_enabled": False,
        "lang": lang,
        "strategy": strategy,
        "files": 0,
        "chunks": 0,
        "root_stats": [],
    }


def _graphrag_stats(artifact: Path, *, lang: str) -> dict:
    """Stats payload for a built ``graphrag`` index.

    LightRAG owns the storage format, so file/chunk counts are reported as
    ``None`` (unknown) rather than fabricated as 0.
    """
    return {
        "db_path": str(artifact),
        "db_exists": True,
        "db_mtime": _file_mtime_iso(artifact),
        "schema_version": "-",
        "fts5_enabled": False,
        "lang": lang,
        "strategy": "graphrag",
        "files": None,
        "chunks": None,
        "root_stats": [],
    }


def resolve_effective_roots(
    repo_root: Path,
    roots: Iterable[str] | None = None,
    *,
    settings_backend: Optional[SettingsBackend] = None,
) -> List[str]:
    """Resolve effective index roots.

    Priority:
      1. Explicit ``roots`` argument when non-empty.
      2. ``[mdq] target_folders`` from the settings backend.
      3. ``mdq_cli.DEFAULT_ROOTS``.
    """
    if roots is not None:
        explicit = [r for r in roots if r]
        if explicit:
            return list(explicit)
    backend: Any = settings_backend or settings_store
    try:
        configured = backend.get_mdq_target_folders(repo_root)
    except Exception:  # pragma: no cover - corrupted settings fallback
        configured = []
    if configured:
        return configured
    return list(mdq_cli.DEFAULT_ROOTS)


def get_index_stats(
    repo_root: Path,
    *,
    db_path: Path | None = None,
    lang: str = "ja-jp",
    strategy: str = "heading",
    settings_backend: Optional[SettingsBackend] = None,
) -> dict:
    """Return index statistics.

    Never creates the index: an unbuilt strategy is reported with
    ``db_exists=False`` and zeroed counters (FR-GUI-05).
    """
    if strategy == "graphrag" and db_path is None:
        artifact = index_artifact_path(repo_root, lang=lang, strategy=strategy)
        if not _index_exists(artifact, strategy):
            return _missing_index_stats(artifact, lang=lang, strategy=strategy)
        return _graphrag_stats(artifact, lang=lang)
    resolved_db = _resolve_db_path(repo_root, db_path, lang=lang, strategy=strategy)
    if not resolved_db.exists():
        return _missing_index_stats(resolved_db, lang=lang, strategy=strategy)
    conn = mdq_store.open_store(resolved_db, lang=lang)
    try:
        base = mdq_store.stats(conn)
        root_stats = []
        for root in resolve_effective_roots(
            repo_root, settings_backend=settings_backend
        ):
            files = conn.execute(
                "SELECT COUNT(*) FROM files WHERE path = ? OR path LIKE ?",
                (root, f"{root}/%"),
            ).fetchone()[0]
            chunks = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE path = ? OR path LIKE ?",
                (root, f"{root}/%"),
            ).fetchone()[0]
            root_stats.append(
                {
                    "root": root,
                    "files": int(files),
                    "chunks": int(chunks),
                }
            )
        return {
            "db_path": str(resolved_db),
            "db_exists": resolved_db.exists(),
            "db_mtime": _file_mtime_iso(resolved_db),
            "schema_version": mdq_store.SCHEMA_VERSION,
            "fts5_enabled": mdq_store.has_fts5(conn),
            "lang": lang,
            "strategy": strategy,
            "files": int(base.get("files", 0)),
            "chunks": int(base.get("chunks", 0)),
            "root_stats": root_stats,
        }
    finally:
        conn.close()


def rebuild_index(
    repo_root: Path,
    *,
    roots: Iterable[str] | None = None,
    db_path: Path | None = None,
    lang: str = "ja-jp",
    strategy: str = "heading",
    overlap_paragraphs: int | None = None,
    force: bool = False,
    semantic_options: dict | None = None,
    pageindex_options: dict | None = None,
    graphrag_options: dict | None = None,
    settings_backend: Optional[SettingsBackend] = None,
    progress_callback=None,
) -> dict:
    """Manually rebuild the index and return a summary.

    ``strategy == "graphrag"`` is delegated to
    :func:`mdq.indexer.build_graphrag_index` (the builder the CLI uses) and
    never touches SQLite.

    Parameters
    ----------
    force:
        When True, passes ``rebuild=True`` to :func:`mdq.indexer.build_index`
        so every file is re-scanned even when SHA-1 matches (Q1=A 完全再ビルド).
    semantic_options:
        When ``strategy == "semantic_paragraph"``, the dict is forwarded to
        :func:`mdq.strategies_semantic.set_runtime_config`. Keys recognised:
        ``max_chars`` / ``min_chars`` / ``percentile_lo`` / ``percentile_hi``
        / ``embed_provider`` / ``embed_model`` / ``contextualize`` /
        ``late_chunking``. Caller should pre-normalise via
        :func:`settings_store.get_semantic_runtime_config`.
    pageindex_options:
        When ``strategy == "pageindex"``, the dict is forwarded to
        :func:`mdq.strategies_pageindex.set_runtime_config`. Keys recognised:
        ``summary_chars`` / ``summary_mode``.
    graphrag_options:
        When ``strategy == "graphrag"``, the dict is applied to
        :class:`mdq.strategies_graphrag.GraphRAGConfig`. Keys recognised:
        ``llm_timeout``.
    progress_callback:
        Optional ``Callable[[str, int, int], None]`` forwarded to the
        indexer. Caller is responsible for thread safety.
    """
    selected_roots = resolve_effective_roots(
        repo_root, roots, settings_backend=settings_backend
    )
    if strategy == "graphrag":
        return _rebuild_graphrag_index(
            repo_root,
            selected_roots,
            lang=lang,
            force=force,
            options=graphrag_options,
            progress_callback=progress_callback,
        )
    resolved_db = _resolve_db_path(repo_root, db_path, lang=lang, strategy=strategy)
    # Install semantic_paragraph runtime overrides BEFORE opening the store
    # so the strategy dispatch picks them up on the first index_one_file call.
    if strategy == "semantic_paragraph":
        try:
            from mdq import strategies_semantic as _sem
            _sem.clear_runtime_config()
            if semantic_options:
                _sem.set_runtime_config(**semantic_options)
        except Exception:  # noqa: BLE001 -- semantic extra not installed
            # The strategy will transparently fall back to heading_recursive.
            pass
    # Install pageindex runtime overrides BEFORE opening the store.
    if strategy == "pageindex":
        try:
            from mdq import strategies_pageindex as _pi
            _pi.clear_runtime_config()
            if pageindex_options:
                _pi.set_runtime_config(**pageindex_options)
        except Exception:  # noqa: BLE001 -- defensive
            pass
    conn = mdq_store.open_store(resolved_db, lang=lang)
    try:
        t0 = perf_counter()
        summary = mdq_indexer.build_index(
            repo_root,
            selected_roots,
            conn,
            rebuild=bool(force),
            prune=True,
            strategy=strategy,
            overlap_paragraphs=overlap_paragraphs,
            progress_callback=progress_callback,
        )
        elapsed_ms = int((perf_counter() - t0) * 1000)
        summary["roots"] = selected_roots
        summary["db_path"] = str(resolved_db)
        summary["lang"] = lang
        summary["strategy"] = strategy
        summary["elapsed_ms"] = elapsed_ms
        summary["force_rebuild"] = bool(force)
        if overlap_paragraphs is not None:
            summary["overlap_paragraphs"] = int(overlap_paragraphs)
        return summary
    finally:
        conn.close()


def _rebuild_graphrag_index(
    repo_root: Path,
    roots: List[str],
    *,
    lang: str,
    force: bool,
    options: dict | None,
    progress_callback,
) -> dict:
    """Build the ``graphrag`` index through the same builder the CLI uses.

    Falling back to :func:`mdq.indexer.build_index` here would write a chunk-less
    SQLite file and never create the LightRAG working directory (FR-GUI-05).
    """
    from mdq import strategies_graphrag as _gr

    _gr.set_runtime_config(_gr.GraphRAGConfig(**(options or {})))
    working_dir = index_artifact_path(repo_root, lang=lang, strategy="graphrag")
    t0 = perf_counter()
    summary = mdq_indexer.build_graphrag_index(
        repo_root,
        roots,
        working_dir,
        rebuild=bool(force),
        progress_callback=progress_callback,
    )
    summary["roots"] = roots
    summary["db_path"] = str(working_dir)
    summary["lang"] = lang
    summary["strategy"] = "graphrag"
    summary["elapsed_ms"] = int((perf_counter() - t0) * 1000)
    summary["force_rebuild"] = bool(force)
    return summary


def delete_index_db(
    repo_root: Path,
    *,
    lang: str = "ja-jp",
    strategy: str = "heading",
    db_path: Path | None = None,
) -> dict:
    """Delete the index of the given (lang, strategy).

    The target is the strategy's own artifact: a SQLite file for every
    strategy except ``graphrag``, whose LightRAG working directory is removed
    recursively.

    Per Q12=B: this operation **only deletes**; it does not recreate an
    empty index. Subsequent ``get_index_stats`` calls return ``db_exists=False``
    until the user explicitly rebuilds.

    Returns ``{"deleted": bool, "db_path": str}``. ``deleted`` is False
    when the artifact did not exist (idempotent no-op).

    Raises :class:`OSError` only when the artifact exists but cannot be removed
    (e.g. another process holds the SQLite lock on Windows). Callers should
    surface this to the user with a remediation hint.
    """
    if db_path is None:
        target = index_artifact_path(repo_root, lang=lang, strategy=strategy)
    else:
        target = db_path
    if not target.exists():
        return {"deleted": False, "db_path": str(target)}
    if target.is_dir():
        # graphrag: the path is derived from store.graphrag_dir_for(), never
        # from user input, so it is always inside .mdq/.
        import shutil

        shutil.rmtree(target)  # may raise OSError on Windows file lock
    else:
        target.unlink()  # may raise OSError on Windows file lock
    return {"deleted": True, "db_path": str(target)}


def search_preview(
    repo_root: Path,
    query: str,
    *,
    lang: str = "ja-jp",
    strategy: str = "heading",
    top_k: int = 3,
    db_path: Path | None = None,
    fusion_alpha: float | None = None,
) -> list[dict]:
    """Run a top-k preview search against the index.

    Used by the GUI "試し検索" panel (Q4=B 折りたたみ). Returns a list of
    dict rows suitable for ``QTableWidget``:
      ``{"path": str, "heading_path": str, "score": float, "snippet": str}``

    Empty list is returned when:
      - the DB file does not exist (the GUI should display "未ビルド"), or
      - the query yields no hits.
    """
    from mdq import search as mdq_search

    resolved_db = _resolve_db_path(
        repo_root, db_path, lang=lang, strategy=strategy
    )
    if not resolved_db.exists():
        return []
    conn = mdq_store.open_store(resolved_db, lang=lang)
    try:
        hits = mdq_search.search(
            conn, query,
            top_k=int(top_k),
            max_tokens=600,
            fusion_alpha=fusion_alpha,
        )
        return [
            {
                "path": h.path,
                "heading_path": h.heading_path or "(top)",
                "score": float(h.score),
                "snippet": h.snippet,
            }
            for h in hits
        ]
    finally:
        conn.close()


def get_index_stats_all_strategies(
    repo_root: Path,
    *,
    lang: str = "ja-jp",
    settings_backend: Optional[SettingsBackend] = None,
) -> dict[str, dict]:
    """Return statistics for every chunking strategy.

    Each strategy is probed at its own index location (``graphrag`` owns a
    LightRAG working directory, the rest own a SQLite file). Unbuilt
    strategies are reported from :func:`_missing_index_stats` so that
    reporting never materialises an index (FR-GUI-05).
    """
    out: dict[str, dict] = {}
    for strategy in ALL_STRATEGIES:
        artifact = index_artifact_path(repo_root, lang=lang, strategy=strategy)
        if not _index_exists(artifact, strategy):
            out[strategy] = _missing_index_stats(
                artifact, lang=lang, strategy=strategy
            )
            continue
        if strategy == "graphrag":
            out[strategy] = _graphrag_stats(artifact, lang=lang)
            continue
        try:
            out[strategy] = get_index_stats(
                repo_root,
                lang=lang,
                strategy=strategy,
                settings_backend=settings_backend,
            )
        except Exception as exc:  # pragma: no cover - defensive
            out[strategy] = {
                "db_path": str(artifact),
                "db_exists": True,  # file exists but is unreadable
                "db_mtime": _file_mtime_iso(artifact),
                "schema_version": "-",
                "fts5_enabled": False,
                "lang": lang,
                "strategy": strategy,
                "files": 0,
                "chunks": 0,
                "root_stats": [],
                "error": str(exc),
            }
    return out
