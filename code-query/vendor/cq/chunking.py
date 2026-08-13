"""Structure-aware chunking and identifier expansion for cq (FR-CQ-05).

Chunking follows cAST (arXiv:2506.15655): large AST nodes are split into their
children and small siblings are merged back together within a size budget, so
that every chunk is a self-contained unit rather than an arbitrary line window.

Identifier expansion makes `resolveUserProfile` reachable from the word query
"user profile" without giving up exact-token search.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from cq import languages
from cq.languages import ChunkSpan

DEFAULT_MAX_CHARS = 1600

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")


@dataclass(frozen=True)
class Chunk:
    start_line: int
    end_line: int
    text: str
    name: str
    signature: str
    ident_text: str


def split_identifier(text: str) -> list[str]:
    """Split camelCase / PascalCase / snake_case into lower-cased words."""
    words: list[str] = []
    for part in text.split("_"):
        words.extend(m.group(0).lower() for m in _WORD_RE.finditer(part))
    return words


def identifier_text(source: str) -> str:
    """Return multi-word identifiers plus their split words, order-stable.

    Single-word identifiers are omitted: they are already reachable through the
    `text` column of the ranking index, so repeating them only inflates the index.
    """
    seen: dict[str, None] = {}
    for match in _IDENTIFIER_RE.finditer(source):
        token = match.group(0)
        words = split_identifier(token)
        if len(words) < 2:
            continue
        seen.setdefault(token, None)
        for word in words:
            seen.setdefault(word, None)
    return " ".join(seen)


def chunk_source(source: str, lang: str, *, max_chars: int = DEFAULT_MAX_CHARS) -> tuple[Chunk, ...]:
    if not source.strip():
        return ()
    lines = source.splitlines()
    support = languages.support_for(lang)
    if support is None or support.chunk is None:
        return _window_chunks(lines, max_chars)
    try:
        spans = support.chunk(source, lines, max_chars)
    except Exception:
        # Same contract as indexer._extract: a chunker failure costs structure
        # for this one file, never the whole index.
        return _window_chunks(lines, max_chars)
    return _materialise(_merge(_normalise(spans, len(lines)), lines, max_chars), lines)


def _normalise(spans: tuple[ChunkSpan, ...], last_line: int) -> list[ChunkSpan]:
    """Turn advisory spans into a gap-free, non-overlapping line partition.

    Syntax-tree siblings routinely share a line (`} else {`), and a node can end
    past the last line when the file ends with a newline, so the core enforces
    the partition rather than trusting each language module to do it.
    """
    partition: list[ChunkSpan] = []
    cursor = 1
    for span in sorted(spans, key=lambda s: (s.start, s.end)):
        if span.end < cursor:
            continue  # 完全に先行 span へ含まれる（入れ子の）span
        start = max(span.start, cursor)
        end = min(max(span.end, start), last_line)
        if start > last_line:
            break
        if start > cursor:
            partition.append(ChunkSpan(cursor, start - 1))
        partition.append(ChunkSpan(start, end, span.name, span.signature))
        cursor = end + 1
    if cursor <= last_line:
        partition.append(ChunkSpan(cursor, last_line))
    return partition


def span_size(lines: list[str], start: int, end: int) -> int:
    """Chunk budget metric, shared by every language module (FR-MAINT-07)."""
    return sum(len(lines[i]) + 1 for i in range(start - 1, min(end, len(lines))))


def _merge(spans: list[ChunkSpan], lines: list[str], max_chars: int) -> list[ChunkSpan]:
    """Merge small siblings, but never start a chunk in the middle of a definition.

    A named definition always opens a new chunk; unnamed material (imports, blank
    regions, module constants) is folded into the preceding chunk while it fits.
    """
    merged: list[ChunkSpan] = []
    for span in sorted(spans, key=lambda s: s.start):
        if not merged or span.name:
            merged.append(span)
            continue
        previous = merged[-1]
        if span.start == previous.end + 1 and span_size(lines, previous.start, span.end) <= max_chars:
            merged[-1] = ChunkSpan(previous.start, span.end, previous.name, previous.signature)
        else:
            merged.append(span)
    return merged


def _materialise(spans: list[ChunkSpan], lines: list[str]) -> tuple[Chunk, ...]:
    chunks: list[Chunk] = []
    for span in spans:
        text = "\n".join(lines[span.start - 1:span.end])
        if not text.strip():
            continue
        chunks.append(Chunk(
            start_line=span.start,
            end_line=span.end,
            text=text,
            name=span.name,
            signature=span.signature,
            ident_text=identifier_text(text),
        ))
    return tuple(chunks)


def _window_chunks(lines: list[str], max_chars: int) -> tuple[Chunk, ...]:
    """Fallback for languages without a structural parser yet (FR-CQ-11)."""
    spans: list[ChunkSpan] = []
    start = 1
    size = 0
    for number, line in enumerate(lines, start=1):
        size += len(line) + 1
        if size >= max_chars:
            spans.append(ChunkSpan(start, number))
            start, size = number + 1, 0
    if start <= len(lines):
        spans.append(ChunkSpan(start, len(lines)))
    return _materialise(spans, lines)
