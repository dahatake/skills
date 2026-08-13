"""Sentence splitter for the `semantic_paragraph` chunking strategy.

Design (per work/semantic-paragraph/plan.md):
- Q3=C: primary tokenizer is `nltk.sent_tokenize` with the `punkt_tab` model.
- Japanese support: nltk Punkt is trained for English and does not treat a
  line break as a sentence boundary, so we cannot signal JA boundaries by
  injecting newlines. Instead we cut the block at Japanese terminators
  (`。` `！` `？` `．`) first and run Punkt inside each piece. Text without
  those terminators reaches Punkt unchanged.
- Fenced code blocks (```` ``` ```` / `~~~`) and pipe-tables are kept atomic:
  we never split inside a fence/table.
- If nltk import or `punkt_tab` download fails, we fall back to a pure-regex
  splitter so that the function never raises (the strategy can still run,
  with slightly lower precision on JA text).

Public API:
- :func:`split_sentences(text: str) -> list[str]`
  Returns the list of sentence strings (whitespace-stripped, empty filtered).
  Order is preserved; concatenating with single spaces does *not* reconstruct
  the original document (line breaks are normalised).
- :func:`split_with_offsets(text: str) -> list[tuple[int, int, str]]`
  Returns `(start_char, end_char, sentence)` triples relative to the
  *input* string. Useful when callers need byte-accurate offsets to map
  sentences back to original line numbers.

This module is import-safe even when the `semantic` extra is not installed:
nltk is imported lazily inside :func:`_nltk_tokenize`.
"""
from __future__ import annotations

import re
from typing import Iterable

# --- Constants -------------------------------------------------------------

# Japanese sentence terminators. Halfwidth `!` `?` are intentionally excluded
# from this set because they are commonly used inside English sentences too;
# nltk handles those correctly.
_JA_TERMINATORS = ("。", "！", "？", "．")

# Zero-width cut just after a JA terminator. Used to hand Punkt one JA
# sentence at a time without altering the text (offsets must stay valid).
_JA_BOUNDARY_RE = re.compile("(?<=[" + "".join(_JA_TERMINATORS) + "])")

# Fence/table detection (line-anchored).
_FENCE_RE = re.compile(r"^(```|~~~)")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")

# Pure-regex fallback splitter (used when nltk is unavailable). Splits at
# `[.!?。！？．]+` followed by whitespace or end-of-string. Conservative: it
# keeps the terminator with the preceding sentence.
_REGEX_SPLIT_RE = re.compile(r"(?<=[.!?。！？．])\s+")


# --- Public API ------------------------------------------------------------


def split_sentences(text: str) -> list[str]:
    """Split *text* into sentences (whitespace-stripped, empties dropped)."""
    return [s for _, _, s in split_with_offsets(text)]


def split_with_offsets(text: str) -> list[tuple[int, int, str]]:
    """Split *text* and return ``(start, end, sentence)`` triples.

    Offsets are character positions into *text* (0-based, end-exclusive).
    Fenced code blocks and pipe-tables are emitted as single atomic
    sentences, regardless of how many newlines they span.
    """
    if not text:
        return []

    blocks = _segment_atomic_blocks(text)
    triples: list[tuple[int, int, str]] = []
    for kind, start, end in blocks:
        block = text[start:end]
        if kind == "atomic":
            stripped = block.strip()
            if stripped:
                # Find the actual stripped span within the block.
                lead = len(block) - len(block.lstrip())
                trail = len(block) - len(block.rstrip())
                triples.append((start + lead, end - trail, stripped))
            continue
        # kind == "prose"
        for ofs, sent in _split_prose_with_offsets(block):
            triples.append((start + ofs, start + ofs + len(sent), sent))
    return triples


# --- Internal: atomic-block segmentation ----------------------------------


def _segment_atomic_blocks(text: str) -> list[tuple[str, int, int]]:
    """Partition *text* into ``("atomic"|"prose", start, end)`` runs.

    Atomic runs are fenced code blocks and contiguous pipe-table rows; they
    must never be split mid-block. Prose runs are everything else.
    """
    if not text:
        return []
    out: list[tuple[str, int, int]] = []
    lines = text.split("\n")
    # Pre-compute char offsets of each line start (inclusive of newline).
    offsets: list[int] = []
    pos = 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln) + 1  # +1 for the '\n' we split on
    # `text` may not end with '\n'; the final offset is len(text).
    offsets.append(len(text))

    i = 0
    n = len(lines)
    prose_start = 0
    fence_marker: str | None = None
    while i < n:
        line = lines[i]
        if fence_marker is None:
            m = _FENCE_RE.match(line)
            if m:
                # Flush any pending prose.
                atomic_start = offsets[i]
                if prose_start < atomic_start:
                    out.append(("prose", prose_start, atomic_start))
                fence_marker = m.group(1)
                # Find the matching closing fence (same marker, possibly with
                # info string difference doesn't matter -- we just match the
                # leading triple).
                j = i + 1
                while j < n and not lines[j].startswith(fence_marker):
                    j += 1
                # Include the closing fence line in the atomic block.
                end_line = j if j < n else n - 1
                fence_end = (
                    offsets[end_line + 1] if end_line + 1 < len(offsets)
                    else len(text)
                )
                out.append(("atomic", atomic_start, fence_end))
                fence_marker = None
                i = end_line + 1
                prose_start = i if i < n else len(text)
                # Convert line index back to char offset for next prose start.
                prose_start = (
                    offsets[i] if i < n else len(text)
                )
                continue
            if _TABLE_ROW_RE.match(line):
                atomic_start = offsets[i]
                if prose_start < atomic_start:
                    out.append(("prose", prose_start, atomic_start))
                j = i
                while j < n and _TABLE_ROW_RE.match(lines[j]):
                    j += 1
                table_end = offsets[j] if j < len(offsets) else len(text)
                out.append(("atomic", atomic_start, table_end))
                i = j
                prose_start = offsets[i] if i < n else len(text)
                continue
        i += 1
    if prose_start < len(text):
        out.append(("prose", prose_start, len(text)))
    return out


# --- Internal: prose splitting (nltk + JA pre-processing) -----------------


def _split_prose_with_offsets(block: str) -> Iterable[tuple[int, str]]:
    """Yield ``(offset_within_block, sentence)`` for a prose run."""
    if not block.strip():
        return
    # JA pre-processing: insert '\n' after every JA terminator that is
    # immediately followed by a non-newline, non-quote character. This
    # gives nltk's Punkt a clear English-like boundary signal without
    # changing the character count *if* we keep a mapping. Since we need
    # offsets, we tokenize the *original* block and rely on substring
    # search to recover offsets.
    sentences = _tokenize_text(block)
    cursor = 0
    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue
        idx = block.find(sent, cursor)
        if idx < 0:
            # Tokenizer modified the string (rare, e.g. quote normalisation).
            # Fall back to advancing the cursor without an exact offset.
            idx = cursor
        yield idx, sent
        cursor = idx + len(sent)


def _tokenize_text(block: str) -> list[str]:
    """Run nltk sent_tokenize; fall back to regex on any failure."""
    try:
        return _nltk_tokenize(block)
    except Exception:
        return _regex_tokenize(block)


_NLTK_READY: bool | None = None


def _nltk_tokenize(block: str) -> list[str]:
    """Tokenize using nltk Punkt, downloading `punkt_tab` on first use."""
    global _NLTK_READY
    import nltk  # type: ignore

    if _NLTK_READY is None:
        try:
            nltk.data.find("tokenizers/punkt_tab")
            _NLTK_READY = True
        except LookupError:
            try:
                nltk.download("punkt_tab", quiet=True)
                _NLTK_READY = True
            except Exception:
                _NLTK_READY = False
    if not _NLTK_READY:
        raise RuntimeError("nltk punkt_tab unavailable")

    # Punkt ignores newlines, so JA boundaries are cut here rather than
    # signalled to it. Nothing is inserted: callers map results back by
    # substring search and need the text to stay identical.
    out: list[str] = []
    for segment in _JA_BOUNDARY_RE.split(block):
        if not segment.strip():
            continue
        out.extend(
            s.strip() for s in nltk.tokenize.sent_tokenize(segment) if s.strip()
        )
    return out


def _regex_tokenize(block: str) -> list[str]:
    """Pure-stdlib fallback splitter."""
    # First, insert newline after JA terminators to normalise.
    pre = block
    for term in _JA_TERMINATORS:
        pre = pre.replace(term, term + "\n")
    parts = _REGEX_SPLIT_RE.split(pre)
    return [p.strip() for p in parts if p.strip()]


__all__ = ["split_sentences", "split_with_offsets"]
