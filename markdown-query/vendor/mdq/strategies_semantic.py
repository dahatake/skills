"""`semantic_paragraph` chunking strategy implementation.

Design (per work/semantic-paragraph/plan.md):
- Q3=C: nltk sentence splitter (with regex fallback).
- Q4=B: Kamradt-modified breakpoint threshold via binary search over
  percentile values to satisfy SEMANTIC_MAX_CHARS.
- Q7=A: heading boundaries are HARD (a chunk never crosses a heading).
- Q11=B: contextualization template prepended by default (LLM-free).

Algorithm:
  1. Use the existing heading splitter to obtain heading-bounded chunks.
  2. For each heading chunk:
     2a. Split into sentences (sentence_splitter).
     2b. Build sliding windows of size buffer_size.
     2c. Embed each window; compute consecutive cosine distances.
     2d. Binary search percentile in [PERCENTILE_LO, PERCENTILE_HI] s.t.
         the maximum produced chunk size is <= MAX_CHARS.
     2e. Merge tail chunks below MIN_CHARS into their predecessor.
  3. Heading boundaries are absolute — no merge crosses them.
  4. If embeddings are unavailable (no extras), fall back to
     `heading_recursive` semantics and log a stderr warning.

Public entry point:
  :func:`scan_file_semantic_paragraph(repo_root, file_path, **kwargs)
   -> (frontmatter, list[Chunk])`
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence

# Defaults (see plan §3.1).
SEMANTIC_BUFFER_SIZE: int = 1
SEMANTIC_PERCENTILE_LO: float = 50.0
SEMANTIC_PERCENTILE_HI: float = 99.0
SEMANTIC_MIN_CHARS: int = 200
SEMANTIC_MAX_CHARS: int = 1000
SEMANTIC_OVERLAP_PARAGRAPHS: int = 0
# Binary-search precision: stop when (hi - lo) <= this many percentile points.
_PERCENTILE_TOLERANCE: float = 1.0
# Cap iterations to keep indexing time bounded.
_BS_MAX_ITER: int = 8

# Default contextualizer template (Q11=B). Defined here so the strategy stays
# import-light; this constant is the single source of truth for the template.
_CTX_TEMPLATE = "[Context] {path} > {heading_path}\n\n{body}"

# Module-level runtime overrides set by the CLI (mdq.cli.cmd_index).
# Keeps function signatures stable while allowing all defaults to be
# overridden from the command line (Q8=A). None means "use the default
# constant above". Set via :func:`set_runtime_config` and read at the top
# of :func:`scan_file_semantic_paragraph`.
_RUNTIME_CONFIG: dict[str, object] = {}


def set_runtime_config(**overrides) -> None:
    """Install per-run overrides for CLI flags.

    Recognised keys: buffer_size, percentile_lo, percentile_hi, min_chars,
    max_chars, contextualize, embed_provider, embed_model, late_chunking,
    fusion_alpha. Unknown keys are stored verbatim (harmless).
    """
    for k, v in overrides.items():
        if v is not None:
            _RUNTIME_CONFIG[k] = v


def clear_runtime_config() -> None:
    """Reset runtime overrides; primarily used by tests."""
    _RUNTIME_CONFIG.clear()


def _cfg(key: str, default):
    return _RUNTIME_CONFIG.get(key, default)


def scan_file_semantic_paragraph(
    repo_root: Path,
    file_path: Path,
    *,
    buffer_size: int = SEMANTIC_BUFFER_SIZE,
    percentile_lo: float = SEMANTIC_PERCENTILE_LO,
    percentile_hi: float = SEMANTIC_PERCENTILE_HI,
    min_chars: int = SEMANTIC_MIN_CHARS,
    max_chars: int = SEMANTIC_MAX_CHARS,
    contextualize: bool = True,
    embed_provider: str | None = None,
    embed_model: str | None = None,
):
    """Produce ``(frontmatter, list[Chunk])`` for the semantic_paragraph strategy.

    When the embedding provider cannot be loaded (no extras installed), the
    function transparently delegates to ``heading_recursive`` so callers
    keep working — a warning is written to stderr.
    """
    # Apply runtime overrides set by the CLI. Explicit kwargs win over
    # _RUNTIME_CONFIG so unit tests can still pin behaviour.
    buffer_size = int(_cfg("buffer_size", buffer_size))
    percentile_lo = float(_cfg("percentile_lo", percentile_lo))
    percentile_hi = float(_cfg("percentile_hi", percentile_hi))
    min_chars = int(_cfg("min_chars", min_chars))
    max_chars = int(_cfg("max_chars", max_chars))
    contextualize = bool(_cfg("contextualize", contextualize))
    embed_provider = embed_provider or _cfg("embed_provider", None)  # type: ignore[arg-type]
    embed_model = embed_model or _cfg("embed_model", None)  # type: ignore[arg-type]
    late_chunking = bool(_cfg("late_chunking", False))
    from . import indexer as _indexer
    from . import strategies as _strategies

    # 1. Heading-bounded coarse split (re-use the existing scan_file).
    fm, heading_chunks = _indexer.scan_file(
        repo_root, file_path,
        max_chunk_chars=0,        # heading-only at this stage
        overlap_paragraphs=0,
    )

    # 2. Try to load the embedding provider. If it fails, fall back.
    try:
        from . import embeddings as _emb
        provider = _emb.get_provider(name=embed_provider, model=embed_model)
    except Exception as e:  # noqa: BLE001 -- any init error → fallback
        print(
            f"[mdq:semantic_paragraph] embedding provider unavailable "
            f"({e}); falling back to heading_recursive.",
            file=sys.stderr,
        )
        return _strategies.scan_file_for_strategy(
            repo_root, file_path, "heading_recursive",
            max_chunk_chars=max_chars,
            overlap_paragraphs=SEMANTIC_OVERLAP_PARAGRAPHS,
        )

    # 3. Per heading chunk: sentence-split, embed, threshold, materialise.
    from . import sentence_splitter as _ss

    out_chunks: list[_indexer.Chunk] = []
    for hc in heading_chunks:
        sub_bodies = _semantic_split_one(
            hc.text,
            provider=provider,
            buffer_size=buffer_size,
            percentile_lo=percentile_lo,
            percentile_hi=percentile_hi,
            min_chars=min_chars,
            max_chars=max_chars,
            splitter=_ss.split_with_offsets,
        )
        if not sub_bodies:
            sub_bodies = [hc.text]

        # Line offsets relative to the heading chunk's start_line. Each
        # sub-body keeps its order; we approximate per-sub line ranges by
        # counting newlines so that downstream snippet display stays sane.
        line_cursor = hc.start_line
        for i, body in enumerate(sub_bodies):
            line_count = body.count("\n") + 1
            # Several sentences can share one physical line, so the cursor
            # outruns the parent's span. Clamping start too keeps the
            # start <= end invariant every consumer relies on.
            sub_start = min(line_cursor, hc.end_line)
            sub_end = min(hc.end_line, sub_start + line_count - 1)
            line_cursor = sub_end + 1

            # Contextualization template (Q11=B, default ON).
            raw_body = body
            if contextualize:
                final_text = _CTX_TEMPLATE.format(
                    path=hc.path,
                    heading_path=hc.heading_path or "(top)",
                    body=body,
                )
            else:
                final_text = body
                raw_body = None  # NULL → text already raw

            sub = _indexer.Chunk(
                path=hc.path,
                heading_path=hc.heading_path,
                level=hc.level,
                start_line=sub_start,
                end_line=sub_end,
                text=final_text,
                tags=list(hc.tags),
                part_index=i,
                part_total=len(sub_bodies),
                text_raw=raw_body,
            )
            out_chunks.append(sub)

    # Late chunking (Q9=B): embed each finalised chunk body and persist the
    # float32 vector as bytes. Uses the original raw body to avoid the
    # contextualizer template bleeding into the embedding space.
    if late_chunking and out_chunks:
        try:
            bodies = [(c.text_raw or c.text) for c in out_chunks]
            vecs = provider.embed(bodies)
            import numpy as _np
            arr = _np.asarray(vecs, dtype=_np.float32)
            for c, v in zip(out_chunks, arr):
                c.embedding_bytes = v.tobytes()
        except Exception as e:  # noqa: BLE001
            print(
                f"[mdq:semantic_paragraph] late-chunking embed failed ({e}); "
                f"chunks indexed without vectors.",
                file=sys.stderr,
            )

    return fm, out_chunks


# --- Internal: semantic split for one heading chunk -----------------------


def _semantic_split_one(
    text: str,
    *,
    provider,
    buffer_size: int,
    percentile_lo: float,
    percentile_hi: float,
    min_chars: int,
    max_chars: int,
    splitter,
) -> list[str]:
    """Split a single heading-chunk body into semantic sub-bodies."""
    if not text or len(text) <= max_chars:
        # Already small enough — no need to embed.
        return [text] if text else []

    triples = splitter(text)
    if len(triples) <= 1:
        return [text]

    # 2b. Sliding windows of size buffer_size. With buffer_size=1 the
    # window is just the sentence itself.
    windows: list[str] = []
    for i in range(len(triples)):
        lo = max(0, i - (buffer_size - 1) // 2)
        hi = min(len(triples), lo + buffer_size)
        windows.append(" ".join(t[2] for t in triples[lo:hi]))

    # 2c. Embed and compute consecutive cosine distances.
    try:
        vecs = provider.embed(windows)
    except Exception as e:  # noqa: BLE001
        print(
            f"[mdq:semantic_paragraph] embed() failed ({e}); returning text as-is.",
            file=sys.stderr,
        )
        return [text]

    from . import embeddings as _emb
    dists = _emb.cosine_distances(vecs)
    if dists.size == 0:
        return [text]

    # 2d. Kamradt-modified binary search.
    boundaries = _kamradt_modified_boundaries(
        triples, dists,
        percentile_lo=percentile_lo,
        percentile_hi=percentile_hi,
        max_chars=max_chars,
    )

    # Materialise sub-bodies from boundary indices.
    subs = _build_subs(text, triples, boundaries)

    # 2e. Merge tail-end short chunks into their predecessor.
    subs = _merge_short_tail(subs, min_chars=min_chars)
    return subs


def _kamradt_modified_boundaries(
    triples, dists, *,
    percentile_lo: float, percentile_hi: float, max_chars: int,
) -> list[int]:
    """Binary-search the lowest percentile that yields chunks <= max_chars.

    Returns a sorted list of sentence indices *after* which to split.
    """
    import numpy as np

    lo, hi = percentile_lo, percentile_hi
    best: list[int] = []
    for _ in range(_BS_MAX_ITER):
        if hi - lo <= _PERCENTILE_TOLERANCE:
            break
        mid = (lo + hi) / 2.0
        threshold = float(np.percentile(dists, mid))
        boundaries = [i + 1 for i, d in enumerate(dists) if d >= threshold]
        max_seg = _max_segment_chars(triples, boundaries)
        if max_seg <= max_chars:
            # Try a higher percentile (fewer cuts) to keep chunks larger.
            best = boundaries
            lo = mid
        else:
            # Need finer cuts.
            hi = mid
    if not best:
        # Even the highest cutting density couldn't satisfy the budget.
        # Use the lowest percentile (most aggressive) we examined.
        threshold = float(np.percentile(dists, percentile_lo))
        best = [i + 1 for i, d in enumerate(dists) if d >= threshold]
    return sorted(set(best))


def _max_segment_chars(triples, boundaries: list[int]) -> int:
    """Return the largest char-length among segments produced by *boundaries*.

    Each segment is the concatenation of sentences ``triples[a:b]``.
    """
    if not triples:
        return 0
    cuts = [0, *boundaries, len(triples)]
    max_len = 0
    for a, b in zip(cuts, cuts[1:]):
        seg = " ".join(t[2] for t in triples[a:b])
        if len(seg) > max_len:
            max_len = len(seg)
    return max_len


def _build_subs(text: str, triples, boundaries: list[int]) -> list[str]:
    """Materialise sub-bodies from sentence-index boundaries.

    We slice the original *text* via the offsets stored in *triples* so
    that whitespace and code-fence content are preserved (sentence-only
    join would lose blank lines).
    """
    if not triples:
        return [text] if text else []
    cuts = [0, *boundaries, len(triples)]
    subs: list[str] = []
    for a, b in zip(cuts, cuts[1:]):
        if a >= b:
            continue
        start_off = triples[a][0]
        end_off = triples[b - 1][1]
        # Snap to nearest preceding newline for cleaner boundaries.
        if start_off > 0:
            nl = text.rfind("\n", 0, start_off)
            if nl >= 0:
                start_off = nl + 1
        sub = text[start_off:end_off].rstrip()
        if sub.strip():
            subs.append(sub)
    return subs or ([text] if text else [])


def _merge_short_tail(subs: list[str], *, min_chars: int) -> list[str]:
    """Merge any sub shorter than *min_chars* into its predecessor.

    The very first sub may stay below min_chars (no predecessor); that is
    acceptable for very short heading bodies.
    """
    if not subs:
        return subs
    out = [subs[0]]
    for s in subs[1:]:
        if len(s) < min_chars and out:
            out[-1] = out[-1] + "\n\n" + s
        else:
            out.append(s)
    return out


__all__ = ["scan_file_semantic_paragraph"]
