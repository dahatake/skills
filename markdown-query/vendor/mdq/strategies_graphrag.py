"""GraphRAG strategy adapter: thin wrapper over LightRAG (lightrag-hku).

This module owns the *only* code in ``mdq`` that imports ``lightrag``. All
imports happen **lazily** inside the public functions so that:

- importing ``mdq`` (or any other strategy) does not trigger LightRAG's
  module-level ``.env`` load (R5),
- importing ``mdq`` does not accept LightRAG's transitive cloud SDK
  surface as a baseline runtime dependency,
- modules under ``lightrag.llm.*`` -- which call ``pipmaster`` to
  auto-install cloud client packages -- are **never** imported anywhere
  in ``mdq`` (R4).

The LLM completion and embedding callables themselves live in
:mod:`mdq.graphrag_runtime`; this module just wires them into LightRAG.

Public API:

- :class:`GraphRAGUnavailable` -- raised when LightRAG is not installed
  or a runtime backend fails.
- :func:`set_runtime_config` / :func:`get_runtime_config` /
  :func:`clear_runtime_config` -- ambient configuration set by the CLI
  before invoking insert/query.
- :func:`insert_paths` -- async: read files and append them to the
  working-dir corpus.
- :func:`insert_paths_sync` -- sync wrapper around :func:`insert_paths`.
- :func:`query` -- async: run a LightRAG query and return the answer text.
- :func:`query_sync` -- sync wrapper around :func:`query`.

GraphRAG bypasses the SQLite index entirely; storage lives in
``<working_dir>`` (defaulted by the indexer/CLI to ``.mdq/graphrag-<lang>/``).
The strategy is intentionally lossy in the sense that source-line
positions are not preserved; LightRAG returns synthesised answers, not
citations into the original Markdown.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

# --- Exceptions ------------------------------------------------------------


class GraphRAGUnavailable(RuntimeError):
    """Raised when LightRAG cannot be loaded or a runtime backend fails.

    The CLI catches this and surfaces a single concise error: ``mdq``'s
    other strategies remain available because LightRAG is an optional
    extra (``pip install -e .[graphrag]``).
    """


# --- R7: query mode allow-list --------------------------------------------

# LightRAG's QueryParam supports {local, global, hybrid, naive, mix, bypass}.
# mdq only exposes ``local`` and ``naive`` because:
# - ``global``/``hybrid``/``mix`` cost significantly more LLM tokens and
#   are easy to invoke accidentally (especially since QueryParam defaults
#   to ``mix``).
# - ``bypass`` skips retrieval entirely and is not useful here.
# Tests and the CLI both rely on this allow-list.
ALLOWED_QUERY_MODES = frozenset({"local", "naive"})


# --- Ambient runtime configuration ----------------------------------------


@dataclass
class GraphRAGConfig:
    """Runtime configuration for the graphrag strategy.

    Populated by the CLI before invoking insert/query. Callers that
    bypass the CLI (e.g. unit tests) can call :func:`set_runtime_config`
    directly.
    """

    # LLM completion ---------------------------------------------------
    llm_provider: str = "ollama"
    llm_base_url: str = "http://127.0.0.1:11434"
    llm_model: str = "qwen2.5:7b"
    # LightRAG issues extraction calls concurrently while Ollama serialises
    # them, so a request's wall clock includes queue wait. 240s (LightRAG's
    # own default) timed out on this repository's documents; 1200s covers the
    # measured worst case with headroom. Also forwarded to LightRAG itself.
    llm_timeout: float = 1200.0
    llm_num_predict: int | None = None

    # Embedding --------------------------------------------------------
    embed_provider: str = "ollama"
    embed_base_url: str = "http://127.0.0.1:11434"
    embed_model: str = "nomic-embed-text"
    embed_timeout: float = 60.0
    embed_mock_dim: int = 64

    # Shared safety / behaviour ---------------------------------------
    allow_remote_ollama: bool = False

    # Chunking (forwarded to LightRAG) --------------------------------
    chunk_token_size: int = 1200
    chunk_overlap_token_size: int = 100

    # Query defaults ---------------------------------------------------
    query_mode: str = "local"


_RUNTIME_CONFIG: GraphRAGConfig | None = None


def set_runtime_config(config: GraphRAGConfig | None) -> None:
    """Install ``config`` as the ambient runtime configuration."""
    global _RUNTIME_CONFIG
    _RUNTIME_CONFIG = config


def get_runtime_config() -> GraphRAGConfig:
    """Return the active runtime configuration, defaulting if unset."""
    return _RUNTIME_CONFIG if _RUNTIME_CONFIG is not None else GraphRAGConfig()


def clear_runtime_config() -> None:
    """Forget any installed runtime configuration (for tests)."""
    global _RUNTIME_CONFIG
    _RUNTIME_CONFIG = None


# --- Internal helpers ------------------------------------------------------


def _validate_query_mode(mode: str) -> str:
    """Reject any mode not in :data:`ALLOWED_QUERY_MODES` (R7)."""
    if mode not in ALLOWED_QUERY_MODES:
        allowed = ", ".join(sorted(ALLOWED_QUERY_MODES))
        raise ValueError(
            f"graphrag: query mode {mode!r} is not allowed; "
            f"expected one of: {allowed}. "
            "Modes 'global', 'hybrid', 'mix', 'bypass' are intentionally disabled."
        )
    return mode


async def _build_rag(working_dir: Path, cfg: GraphRAGConfig):
    """Build and initialise a LightRAG instance (lazy-imported).

    Returns the initialised ``LightRAG`` instance. The caller is
    responsible for calling ``await rag.finalize_storages()`` when done.
    """
    # R5: lazy import keeps mdq import side-effect free.
    try:
        from lightrag import LightRAG  # type: ignore
        from lightrag.utils import EmbeddingFunc  # type: ignore
        from lightrag.kg.shared_storage import (  # type: ignore
            finalize_share_data,
        )
    except ImportError as e:
        raise GraphRAGUnavailable(
            f"graphrag: LightRAG is not installed: {e}. "
            "Install with: pip install -e .[graphrag]"
        ) from e

    # LightRAG keeps storage state per process. Without dropping it, a second
    # session in the same process (e.g. the GUI's 完全再ビルド) reports success
    # while writing almost nothing to disk. This resets process-global state,
    # so sessions must not overlap within one process.
    finalize_share_data()

    # R4: import only from mdq.graphrag_runtime (never lightrag.llm.*).
    from mdq.graphrag_runtime import (
        GraphRAGRuntimeUnavailable,
        get_completion_func,
        get_embedding_func,
    )

    try:
        completion_fn = get_completion_func(
            cfg.llm_provider,
            base_url=cfg.llm_base_url,
            model=cfg.llm_model,
            timeout=cfg.llm_timeout,
            allow_remote=cfg.allow_remote_ollama,
            num_predict=cfg.llm_num_predict,
        )
        embed_fn, embed_dim = get_embedding_func(
            cfg.embed_provider,
            base_url=cfg.embed_base_url,
            model=cfg.embed_model,
            timeout=cfg.embed_timeout,
            allow_remote=cfg.allow_remote_ollama,
            mock_dim=cfg.embed_mock_dim,
        )
        # Ollama loads the model on first use (~2 min for a 7B model measured
        # 2026-08-14). LightRAG issues extraction calls concurrently while
        # Ollama serialises them, so an unprimed load lands inside a queued
        # request and trips LightRAG's own worker timeout. Pay it once here,
        # where a missing model also surfaces immediately.
        await completion_fn("ok")
    except GraphRAGRuntimeUnavailable as e:
        raise GraphRAGUnavailable(str(e)) from e
    except ValueError as e:
        # Unknown provider names, non-loopback URLs without --allow-remote,
        # zero-dim embeddings, etc. all bubble up as ValueError from the
        # runtime helpers; normalise to GraphRAGUnavailable so the CLI has
        # a single exception type to catch.
        raise GraphRAGUnavailable(f"graphrag: configuration error: {e}") from e

    working_dir.mkdir(parents=True, exist_ok=True)
    rag = LightRAG(
        working_dir=str(working_dir),
        llm_model_func=completion_fn,
        # Ollama processes one request at a time by default
        # (OLLAMA_NUM_PARALLEL=1), so LightRAG's defaults only make the extra
        # requests wait in Ollama's queue with the wait counted against their
        # own timeout. Serialising both layers costs no throughput here.
        llm_model_max_async=1,
        max_parallel_insert=1,
        # LightRAG aborts calls on its own timeouts, so raising only the HTTP
        # timeouts leaves the configured values without effect.
        default_llm_timeout=int(cfg.llm_timeout),
        default_embedding_timeout=int(cfg.embed_timeout),
        embedding_func=EmbeddingFunc(
            embedding_dim=embed_dim,
            max_token_size=8192,
            func=embed_fn,
        ),
        chunk_token_size=cfg.chunk_token_size,
        chunk_overlap_token_size=cfg.chunk_overlap_token_size,
    )
    await rag.initialize_storages()
    return rag


async def _safe_finalize(rag) -> None:
    """Best-effort finalize; never re-raise (caller already has a result)."""
    try:
        await rag.finalize_storages()
    except Exception:  # noqa: BLE001 - finalize errors must not mask insert/query
        pass


# --- Public API: insert ----------------------------------------------------


async def insert_paths(
    working_dir: Path | str,
    paths: Sequence[Path | str],
    *,
    config: GraphRAGConfig | None = None,
    progress_callback: Callable[[str, int, int, str], None] | None = None,
) -> dict[str, str]:
    """Insert each file in ``paths`` into the LightRAG corpus.

    Returns a ``{path: status}`` dict where ``status`` is ``"ok"`` or an
    error message string. Inserts are batched in a single LightRAG
    session so storage is initialised/finalised once.

    ``progress_callback`` (optional): if provided, called after each file
    with ``(path_str, current, total, status)``. ``current`` is 1-based.
    Callback exceptions are swallowed so they cannot abort the batch.
    """
    cfg = config if config is not None else get_runtime_config()
    wd = Path(working_dir)
    rag = await _build_rag(wd, cfg)
    results: dict[str, str] = {}
    # Lazy import here only for the runtime exception type; lightrag itself
    # is already loaded via _build_rag at this point.
    from mdq.graphrag_runtime import GraphRAGRuntimeUnavailable
    total = len(paths)
    try:
        for idx, p in enumerate(paths, start=1):
            path = Path(p)
            try:
                # Read inside the per-file try so PermissionError / missing
                # files surface as a per-file status, not a batch abort.
                if not path.exists():
                    results[str(path)] = "error: file not found"
                    if progress_callback is not None:
                        try:
                            progress_callback(str(path), idx, total, results[str(path)])
                        except Exception:  # noqa: BLE001
                            pass
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                results[str(path)] = f"error: read failed: {e}"
                if progress_callback is not None:
                    try:
                        progress_callback(str(path), idx, total, results[str(path)])
                    except Exception:  # noqa: BLE001
                        pass
                continue
            if not text.strip():
                results[str(path)] = "skipped: empty"
            else:
                try:
                    # ainsert accepts str | list[str]; we insert one file at a
                    # time so per-file errors don't abort the whole batch.
                    await rag.ainsert(text, file_paths=str(path))
                    results[str(path)] = "ok"
                except GraphRAGRuntimeUnavailable as e:
                    results[str(path)] = f"error: {e}"
                except Exception as e:  # noqa: BLE001
                    results[str(path)] = f"error: {e}"
            if progress_callback is not None:
                try:
                    progress_callback(str(path), idx, total, results[str(path)])
                except Exception:  # noqa: BLE001
                    pass
    finally:
        await _safe_finalize(rag)
    return results


def insert_paths_sync(
    working_dir: Path | str,
    paths: Sequence[Path | str],
    *,
    config: GraphRAGConfig | None = None,
    progress_callback: Callable[[str, int, int, str], None] | None = None,
) -> dict[str, str]:
    """Synchronous wrapper around :func:`insert_paths`."""
    return asyncio.run(
        insert_paths(working_dir, paths, config=config, progress_callback=progress_callback)
    )


# --- Public API: query -----------------------------------------------------


async def query(
    working_dir: Path | str,
    query_text: str,
    *,
    mode: str | None = None,
    top_k: int | None = None,
    config: GraphRAGConfig | None = None,
) -> str:
    """Run a LightRAG query and return the answer string."""
    cfg = config if config is not None else get_runtime_config()
    effective_mode = _validate_query_mode(mode or cfg.query_mode)

    # R5: lazy import.
    try:
        from lightrag.base import QueryParam  # type: ignore
    except ImportError as e:
        raise GraphRAGUnavailable(
            f"graphrag: LightRAG is not installed: {e}. "
            "Install with: pip install -e .[graphrag]"
        ) from e

    wd = Path(working_dir)
    if not wd.exists():
        raise GraphRAGUnavailable(
            f"graphrag: working_dir {wd} does not exist. Run `mdq index` first."
        )
    rag = await _build_rag(wd, cfg)
    # Lazy import for the runtime exception type only.
    from mdq.graphrag_runtime import GraphRAGRuntimeUnavailable
    try:
        # R7: mode is explicit; do NOT rely on QueryParam's "mix" default.
        # Also disable rerank by default: LightRAG enables it via env var
        # ``RERANK_BY_DEFAULT=true`` and warns on every query when no
        # rerank model is configured. mdq does not wire one up, so opt out
        # to keep the log clean unless a caller explicitly re-enables it.
        param_kwargs: dict[str, Any] = {"mode": effective_mode, "enable_rerank": False}
        if top_k is not None:
            param_kwargs["top_k"] = int(top_k)
        param = QueryParam(**param_kwargs)
        try:
            result = await rag.aquery(query_text, param=param)
        except GraphRAGRuntimeUnavailable as e:
            # Backend failure during retrieval/generation: surface as the
            # adapter-level error type so the CLI has a single exception
            # to handle.
            raise GraphRAGUnavailable(str(e)) from e
        return str(result) if result is not None else ""
    finally:
        await _safe_finalize(rag)


def query_sync(
    working_dir: Path | str,
    query_text: str,
    *,
    mode: str | None = None,
    top_k: int | None = None,
    config: GraphRAGConfig | None = None,
) -> str:
    """Synchronous wrapper around :func:`query`."""
    return asyncio.run(
        query(
            working_dir,
            query_text,
            mode=mode,
            top_k=top_k,
            config=config,
        )
    )
