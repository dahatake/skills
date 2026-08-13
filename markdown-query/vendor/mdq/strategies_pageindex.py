"""``pageindex`` chunking strategy implementation.

PageIndex-inspired strategy: build a heading-based tree index and attach a
short ``summary`` to each chunk. Unlike Vectify AI's original PageIndex
(which uses an LLM to walk the tree at query time), this implementation is
**Index-only** — no LLM calls, no embeddings. The summary is a deterministic
extract from the chunk body, suitable for table-of-contents navigation via
``expansion.tree_path`` in :mod:`mdq.search`.

Design (per work/pageindex/decisions.md):
- A=Index-only: no LLM/embeddings at index or query time.
- B=head/first_paragraph: deterministic summary from the chunk body.
- C=SCHEMA v6: summary stored in ``chunks.summary``.
- E=fallback: when a file has zero headings, behaves like ``heading``
  (single chunk; the summary is taken from the body head).

Public entry point:
  :func:`scan_file_pageindex(repo_root, file_path) -> (frontmatter, list[Chunk])`

Runtime configuration:
  CLI flags ``--pageindex-summary-chars`` and ``--pageindex-summary-mode``
  install per-run overrides via :func:`set_runtime_config`. Stable defaults
  live in :mod:`mdq.strategies` (``PAGEINDEX_SUMMARY_CHARS`` etc.).
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from . import strategies as _strategies

SummaryMode = Literal["head", "first_paragraph"]

# Module-level runtime overrides set by the CLI (mdq.cli.cmd_index).
_RUNTIME_CONFIG: dict[str, object] = {}


def set_runtime_config(**overrides) -> None:
    """Install per-run overrides for CLI flags.

    Recognised keys: ``summary_chars`` (int), ``summary_mode`` (``head`` /
    ``first_paragraph``). Unknown keys are stored verbatim (harmless).
    ``None`` values are skipped so the caller can pass argparse defaults
    safely. ``summary_chars <= 0`` is **also** skipped so callers can
    forward a 0 sentinel that means "use the code default" without having
    to remap it themselves.
    """
    for k, v in overrides.items():
        if v is None:
            continue
        if k == "summary_chars":
            try:
                if int(v) <= 0:
                    continue
            except (TypeError, ValueError):
                continue
        _RUNTIME_CONFIG[k] = v


def clear_runtime_config() -> None:
    """Reset runtime overrides; primarily used by tests."""
    _RUNTIME_CONFIG.clear()


def _cfg(key: str, default):
    return _RUNTIME_CONFIG.get(key, default)


def _summarize(body: str, mode: SummaryMode, max_chars: int) -> str:
    """Produce a deterministic summary from a chunk body.

    - ``head``: first ``max_chars`` characters of the body (after stripping
      leading whitespace).
    - ``first_paragraph``: the first non-empty paragraph (blank-line
      separated), clipped to ``max_chars``.

    Returns an empty string when the body is empty after stripping.
    """
    if max_chars <= 0:
        return ""
    # Normalise CRLF up front so the character count and slicing are
    # consistent across both summary modes.
    s = body.replace("\r\n", "\n").lstrip()
    if not s:
        return ""
    if mode == "first_paragraph":
        # Split on the first blank-line boundary.
        idx = s.find("\n\n")
        para = s if idx < 0 else s[:idx]
        s = para.strip()
    # Clip to max_chars without splitting mid-surrogate (Python strings are
    # already code-point indexed, so a simple slice is safe).
    if len(s) > max_chars:
        return s[:max_chars].rstrip()
    return s


def scan_file_pageindex(repo_root: Path, file_path: Path):
    """Heading-based chunks plus a per-chunk ``summary`` field.

    Reuses :func:`mdq.indexer.scan_file` with ``max_chunk_chars=0`` (no
    secondary split), then attaches a summary to each :class:`Chunk` via
    its ``summary`` attribute. Files with zero headings produce a single
    chunk (legacy ``heading`` behaviour); the summary is computed from
    that chunk body.
    """
    from . import indexer as _indexer

    fm, chunks = _indexer.scan_file(
        repo_root, file_path, max_chunk_chars=0, overlap_paragraphs=0,
    )
    summary_chars = int(
        _cfg("summary_chars", _strategies.PAGEINDEX_SUMMARY_CHARS)
    )
    mode_raw = str(_cfg("summary_mode", _strategies.PAGEINDEX_SUMMARY_MODE))
    mode: SummaryMode = (
        "first_paragraph" if mode_raw == "first_paragraph" else "head"
    )
    for c in chunks:
        # Strip the heading line itself from the summary source so the
        # extract is the section body, not the heading marker.
        # NOTE: We only need to handle ATX headings (lines starting with
        # '#'). Setext headings (``====`` / ``----`` underlines) are not
        # produced by ``mdq.indexer.HEADING_RE`` so they never become a
        # chunk's first line; no extra handling is required.
        body = c.text
        lines = body.splitlines()
        if lines and lines[0].lstrip().startswith("#"):
            body = "\n".join(lines[1:])
        c.summary = _summarize(body, mode, summary_chars)
    return fm, chunks
