"""Markdown chunking strategies for the markdown-query Skill.

Each (language, strategy) combination is materialised into a separate SQLite
DB under ``.mdq/index-<lang>-<strategy>.sqlite`` (see :func:`mdq.store.db_path_for`).
The ``lang`` axis governs **two** things: (1) the per-language DB file
separation (so a single physical store is queried with a single tokenizer),
and (2) the FTS5 tokenizer choice in :mod:`mdq.tokenize` (``trigram``
for ``ja-jp`` when available; ``unicode61`` otherwise). The chunking
algorithm itself does not depend on ``lang``.

Available strategies (see ``users-guide/skills-markdown-query.md``):

- ``heading`` (default, legacy): heading-bounded chunks via
  :func:`mdq.indexer.scan_file` with ``max_chunk_chars=0``.
- ``heading_recursive``: heading-bounded chunks, then any chunk exceeding
  :data:`HEADING_RECURSIVE_MAX_CHARS` is subdivided via the existing
  paragraph/line/fence-aware splitter (``_subdivide``).
- ``fixed_window``: ignores heading structure, splits the whole file body
  into fixed-size overlapping windows (RecursiveCharacterTextSplitter-style).
- ``semantic_paragraph``: heading-bounded chunks subdivided by embedding
  similarity breakpoints (Kamradt-modified). Requires the ``[semantic]``
  extra; falls back to ``heading_recursive`` if embeddings are unavailable.
- ``pageindex``: heading-bounded chunks (same boundaries as ``heading``)
  plus a per-chunk ``summary`` field computed from the chunk body
  (head N chars or first paragraph). LLM-free; stored in ``chunks.summary``
  (SCHEMA v6). Designed for PageIndex-style table-of-contents navigation.
- ``graphrag``: GraphRAG (knowledge graph + entity/relation extraction) via
  the optional ``[graphrag]`` extra (lightrag-hku + local Ollama). Persists
  outside the SQLite store as ``.mdq/graphrag-<lang>/`` (NetworkX + JSON,
  managed by LightRAG). Requires a running local Ollama server (loopback
  only); falls back to ``heading_recursive`` when unavailable. Auto-router
  does not select this strategy; must be explicitly requested with
  ``--strategy graphrag``.

Future: ``sentence`` (proposition-level) is reserved for Phase 3.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

Strategy = Literal[
    "heading", "heading_recursive", "fixed_window",
    "semantic_paragraph", "pageindex", "graphrag",
]

ALL_STRATEGIES: tuple[Strategy, ...] = (
    "heading",
    "heading_recursive",
    "fixed_window",
    "semantic_paragraph",
    "pageindex",
    "graphrag",
)
DEFAULT_STRATEGY: Strategy = "heading"

# heading_recursive: heading chunks larger than this are subdivided.
HEADING_RECURSIVE_MAX_CHARS: int = 2000

# heading_recursive: when subdividing, prepend the trailing N paragraphs of
# the previous sub-chunk to the next sub-chunk. 0 disables overlap. Fenced
# code blocks are never duplicated by overlap.
HEADING_RECURSIVE_OVERLAP_PARAGRAPHS: int = 1

# fixed_window: window size and overlap in characters.
FIXED_WINDOW_CHARS: int = 1000
FIXED_WINDOW_OVERLAP: int = 200

# pageindex: per-chunk summary length and extraction mode.
PAGEINDEX_SUMMARY_CHARS: int = 200
PAGEINDEX_SUMMARY_MODE: Literal["head", "first_paragraph"] = "head"


def normalize(strategy: str | None) -> Strategy:
    """Return a validated strategy name; falls back to default."""
    if strategy in ALL_STRATEGIES:
        return strategy  # type: ignore[return-value]
    return DEFAULT_STRATEGY


def scan_file_for_strategy(
    repo_root: Path,
    file_path: Path,
    strategy: Strategy,
    *,
    max_chunk_chars: int = 0,
    overlap_paragraphs: int | None = None,
):
    """Produce (frontmatter_dict, list[Chunk]) for the given strategy.

    ``max_chunk_chars`` only applies to ``heading_recursive``. When it is
    ``0`` (the default), :data:`HEADING_RECURSIVE_MAX_CHARS` is used.
    ``overlap_paragraphs`` (heading_recursive only) defaults to
    :data:`HEADING_RECURSIVE_OVERLAP_PARAGRAPHS` when ``None``. Set to 0 to
    disable overlap explicitly.
    """
    from . import indexer as _indexer

    if strategy == "heading":
        return _indexer.scan_file(repo_root, file_path,
                                  max_chunk_chars=max_chunk_chars)
    if strategy == "heading_recursive":
        budget = max_chunk_chars if max_chunk_chars > 0 else HEADING_RECURSIVE_MAX_CHARS
        overlap = (overlap_paragraphs
                   if overlap_paragraphs is not None
                   else HEADING_RECURSIVE_OVERLAP_PARAGRAPHS)
        return _indexer.scan_file(
            repo_root, file_path,
            max_chunk_chars=budget,
            overlap_paragraphs=int(overlap),
        )
    if strategy == "fixed_window":
        return _scan_fixed_window(repo_root, file_path)
    if strategy == "semantic_paragraph":
        from . import strategies_semantic as _sem
        return _sem.scan_file_semantic_paragraph(
            repo_root, file_path,
            max_chars=(max_chunk_chars if max_chunk_chars > 0
                       else _sem.SEMANTIC_MAX_CHARS),
        )
    if strategy == "pageindex":
        from . import strategies_pageindex as _pi
        return _pi.scan_file_pageindex(repo_root, file_path)
    if strategy == "graphrag":
        # graphrag does not produce chunk rows for the SQLite store; the
        # entire pipeline is owned by LightRAG and persists under
        # .mdq/graphrag-<lang>/ via mdq.strategies_graphrag.build_graphrag_index.
        # Callers must dispatch graphrag at the indexer level (see
        # mdq/indexer.py) and never reach this branch in normal flow; the
        # safe fallback below ensures back-compatibility if it does.
        return fm_empty_fallback(repo_root, file_path)
    return _indexer.scan_file(repo_root, file_path, max_chunk_chars=0)


def fm_empty_fallback(repo_root: Path, file_path: Path):
    """Return an empty (frontmatter, chunks) tuple.

    Used by ``graphrag`` strategy as a defensive no-op when the caller
    inadvertently reaches the per-file chunking branch. The actual
    GraphRAG index is built outside the chunk-row pipeline.
    """
    from . import indexer as _indexer
    text = file_path.read_text(encoding="utf-8", errors="replace")
    fm, _ = _indexer._parse_frontmatter(text)
    return fm, []


def _scan_fixed_window(repo_root: Path, file_path: Path):
    """Fixed-size sliding-window chunker.

    The whole file body (after frontmatter) is concatenated and split into
    overlapping character windows. Heading structure is ignored. Each chunk
    receives ``heading_path='(window)'`` and ``level=0`` so the existing
    schema is satisfied without ambiguity.
    """
    from . import indexer as _indexer

    text = file_path.read_text(encoding="utf-8", errors="replace")
    fm, body_offset = _indexer._parse_frontmatter(text)
    lines = text.splitlines()
    body_lines = lines[body_offset:]
    body_text = "\n".join(body_lines)
    rel = file_path.relative_to(repo_root).as_posix()
    raw_tags = fm.get("tags") if isinstance(fm, dict) else None
    if isinstance(raw_tags, list):
        tags = [str(t) for t in raw_tags]
    elif isinstance(raw_tags, str):
        tags = [str(raw_tags)]
    else:
        tags = []

    chunks: list[_indexer.Chunk] = []
    if not body_text.strip():
        return fm, chunks

    win = FIXED_WINDOW_CHARS
    overlap = FIXED_WINDOW_OVERLAP
    step = max(1, win - overlap)
    n = len(body_text)
    parts: list[tuple[str, int]] = []  # (window_text, char_offset)
    start = 0
    while start < n:
        end = min(n, start + win)
        parts.append((body_text[start:end], start))
        if end >= n:
            break
        start += step

    # Map character offsets back to (start_line, end_line) using a cumulative
    # line-length table for the body. 1-based line numbers in the original
    # file: body line k corresponds to file line (body_offset + k).
    cum: list[int] = [0]
    for ln in body_lines:
        cum.append(cum[-1] + len(ln) + 1)  # +1 for '\n'

    def _char_to_line(off: int) -> int:
        # binary search-free linear scan is fine for small files; switch to
        # bisect if profiling shows it matters.
        for k in range(1, len(cum)):
            if cum[k] > off:
                return body_offset + k  # 1-based file line
        return body_offset + len(body_lines)

    total = len(parts)
    for i, (win_text, off) in enumerate(parts):
        s_line = _char_to_line(off)
        e_line = _char_to_line(off + max(0, len(win_text) - 1))
        c = _indexer.Chunk(
            path=rel,
            heading_path="(window)",
            level=0,
            start_line=s_line,
            end_line=e_line,
            text=win_text,
            tags=list(tags),
            part_index=i,
            part_total=total,
        )
        # occurrence_index disambiguates duplicate heading_path within a file.
        c.occurrence_index = 0
        chunks.append(c)
    return fm, chunks
