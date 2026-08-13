"""BM25 + grep search over the indexed chunks.

Uses rank_bm25 when available; otherwise falls back to a tiny stdlib BM25.
Returns hits with minimal snippets (default ±2 body lines around the
strongest match) to keep context windows small.
"""
from __future__ import annotations

import fnmatch
import json
import math
import re
from dataclasses import dataclass
from typing import Iterable

try:
    from rank_bm25 import BM25Okapi  # type: ignore
    HAS_RANK_BM25 = True
except Exception:
    HAS_RANK_BM25 = False

from . import tokenize as _tokenize

# Excerpt-selection tokenizer (layer 2). The CJK range is owned by
# mdq.tokenize so that it cannot drift from the scoring tokenizer.
_TOKEN_RE = re.compile(
    r"[A-Za-z0-9_]+|["
    + "".join(f"{low}-{high}" for low, high in _tokenize.CJK_CHAR_RANGES)
    + "]"
)

# SQLite の trigram トークナイザは 3 文字未満の語を索引しない。
_MIN_TRIGRAM_CHARS = 3

# FR-MDQ-06: BM25 の文書長正規化係数。両実装へ同じ値を適用する。
# 0.2 は、開発用とホールドアウトの 2 つのゴールデン集で filtered / broad の
# いずれも従来値 0.75 を下回らなかった唯一の候補（FR-MDQ-04 の計測による）。
LENGTH_NORM_B = 0.2

# FR-MDQ-08 (3): 見出し経路を本文より重く扱う倍率。
# 暫定値。FR-MDQ-08 (6) の 4 スライス計測で確定するまでは 3 を用いる。
HEADING_WEIGHT = 3


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


@dataclass
class Hit:
    chunk_id: str
    path: str
    heading_path: str
    start_line: int
    end_line: int
    score: float
    snippet: str | None
    expansion: dict | None = None

    def to_dict(self) -> dict:
        d: dict = {
            "chunk_id": self.chunk_id,
            "path": self.path,
            "heading_path": self.heading_path,
            "lines": [self.start_line, self.end_line],
            "score": round(self.score, 4),
        }
        if self.snippet is not None:
            d["snippet"] = self.snippet
        if self.expansion is not None:
            d["expansion"] = self.expansion
        return d


class _MiniBM25:
    """Tiny BM25-Okapi fallback (no external deps)."""

    def __init__(self, corpus: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.N = len(corpus)
        self.avgdl = sum(len(d) for d in corpus) / self.N if self.N else 0
        self.doc_len = [len(d) for d in corpus]
        self.tf: list[dict[str, int]] = []
        df: dict[str, int] = {}
        for doc in corpus:
            counts: dict[str, int] = {}
            for tok in doc:
                counts[tok] = counts.get(tok, 0) + 1
            self.tf.append(counts)
            for tok in counts:
                df[tok] = df.get(tok, 0) + 1
        self.idf = {
            tok: math.log(1 + (self.N - n + 0.5) / (n + 0.5))
            for tok, n in df.items()
        }

    def get_scores(self, query: list[str]) -> list[float]:
        scores = [0.0] * self.N
        for i in range(self.N):
            dl = self.doc_len[i]
            denom_norm = 1 - self.b + self.b * (dl / self.avgdl if self.avgdl else 1)
            for tok in query:
                if tok not in self.idf:
                    continue
                f = self.tf[i].get(tok, 0)
                if f == 0:
                    continue
                scores[i] += self.idf[tok] * (f * (self.k1 + 1)) / (
                    f + self.k1 * denom_norm
                )
        return scores


def _make_snippet(text: str, query_tokens: list[str], radius: int = 2,
                  max_chars: int = 400) -> str:
    """Return a compact snippet centered on the strongest matching line."""
    lines = text.splitlines()
    if not lines:
        return ""
    qset = set(query_tokens)
    best_idx = 0
    best_score = -1
    for i, line in enumerate(lines):
        toks = set(tokenize(line))
        score = len(toks & qset)
        if score > best_score:
            best_score = score
            best_idx = i
    lo = max(0, best_idx - radius)
    hi = min(len(lines), best_idx + radius + 1)
    snippet = "\n".join(lines[lo:hi])
    if len(snippet) > max_chars:
        snippet = snippet[: max_chars - 1] + "…"
    return snippet


def _excerpt(text: str, query_tokens: list[str], radius: int,
             return_unit: str) -> str | None:
    """Return the hit excerpt at the requested granularity (FR-MDQ-03 / FR-MDQ-10)."""
    if return_unit == "locations":
        return None
    if return_unit == "chunk":
        return text
    return _make_snippet(text, query_tokens, radius=radius)


def _path_matches(path: str, globs: list[str]) -> bool:
    if not globs:
        return True
    return any(fnmatch.fnmatch(path, g) for g in globs)


def _tag_matches(tags_json: str | None, wanted: list[str]) -> bool:
    if not wanted:
        return True
    if not tags_json:
        return False
    try:
        tags = json.loads(tags_json)
    except Exception:
        return False
    if not isinstance(tags, list):
        return False
    tagset = {str(t).lower() for t in tags}
    return all(w.lower() in tagset for w in wanted)


def _exact_identifier_tag_priority(tags_json: str | None, query: str) -> int:
    """Prefer rows whose machine tag value is an exact identifier in query.

    Tabular chunks expose the first three columns as ``key=value`` tags.  An
    exact primary ID such as ``FR-MDQ-01`` must outrank rows that merely refer
    to that ID repeatedly in free text.  Generic values (``FR``, ``active``)
    are deliberately excluded: a prioritised value must contain letters,
    digits, and an identifier separator.
    """
    if not tags_json:
        return 0
    try:
        tags = json.loads(tags_json)
    except Exception:
        return 0
    if not isinstance(tags, list):
        return 0
    for raw in tags:
        text = str(raw)
        if "=" not in text:
            continue
        value = text.split("=", 1)[1].strip()
        if (
            len(value) < 4
            or not any(char.isalpha() for char in value)
            or not any(char.isdigit() for char in value)
            or not any(char in "-_/.:" for char in value)
        ):
            continue
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(value)}(?![A-Za-z0-9_])"
        if re.search(pattern, query, re.IGNORECASE):
            return 1
    return 0


def _sort_scored(scored, query: str) -> None:
    """Sort by exact machine-tag ID, then score, then stable location."""
    scored.sort(key=lambda item: (
        -_exact_identifier_tag_priority(_row_get(item[1], "tags"), query),
        -float(item[0]),
        str(_row_get(item[1], "path") or ""),
        int(_row_get(item[1], "start_line") or 0),
        str(_row_get(item[1], "chunk_id") or ""),
    ))


def _maybe_apply_fusion(rows, bm25_scores, query: str,
                        fusion_alpha: float | None):
    """Blend BM25 with dense cosine_sim when late-chunking embeddings exist.

    Returns the (possibly modified) score array. Behaviour:
      * fusion_alpha is None         → return bm25_scores unchanged.
      * No row carries an embedding  → return bm25_scores unchanged.
      * Otherwise: clamp alpha to [0, 1], embed query, compute cosine_sim
        for rows that have an embedding (0.0 for rows that don't), then
        return ``alpha * bm25_norm + (1 - alpha) * cosine_sim`` row-wise.
    Logs a one-line stderr warning if the embedding provider is
    unavailable, or if the stored vectors have a different dimension than
    the query vector (i.e. the index was built with another embedding
    model); falls back to bm25_scores in both cases.
    """
    if fusion_alpha is None:
        return bm25_scores
    # Check whether any row has an embedding.
    have_emb = any(_row_has_embedding(r) for r in rows)
    if not have_emb:
        return bm25_scores
    alpha = max(0.0, min(1.0, float(fusion_alpha)))
    try:
        import numpy as np
        from . import embeddings as _emb
        provider = _emb.get_provider()
        q_vec = provider.embed([query])[0]
    except Exception as e:  # noqa: BLE001
        import sys as _sys
        print(
            f"[mdq:search] fusion disabled (embedding provider unavailable: {e})",
            file=_sys.stderr,
        )
        return bm25_scores

    import numpy as np
    bm = np.asarray(bm25_scores, dtype=np.float32)
    if bm.size and float(bm.max()) > float(bm.min()):
        bm_norm = (bm - float(bm.min())) / (float(bm.max()) - float(bm.min()))
    else:
        bm_norm = np.zeros_like(bm)

    q_norm = float(np.linalg.norm(q_vec)) or 1.0
    cos = np.zeros(len(rows), dtype=np.float32)
    for i, r in enumerate(rows):
        emb_bytes = _row_get(r, "chunk_embedding")
        if not emb_bytes:
            continue
        v = np.frombuffer(emb_bytes, dtype=np.float32)
        if v.shape[0] != len(q_vec):
            # The index records no model name, so a model change is only
            # detectable here. Degrade rather than raise (see docstring).
            import sys as _sys
            print(
                f"[mdq:search] fusion disabled (embedding dim mismatch: "
                f"query={len(q_vec)} index={v.shape[0]}; the index was "
                f"built with a different MDQ_EMBED_MODEL)",
                file=_sys.stderr,
            )
            return bm25_scores
        n = float(np.linalg.norm(v))
        if n == 0:
            continue
        cos[i] = float(np.dot(q_vec, v) / (q_norm * n))
    cos = np.clip(cos, 0.0, 1.0)
    fused = alpha * bm_norm + (1.0 - alpha) * cos
    return fused


def _row_has_embedding(row) -> bool:
    try:
        v = row["chunk_embedding"]
    except Exception:
        return False
    return bool(v)


def _row_get(row, key: str):
    try:
        return row[key]
    except Exception:
        return None


def _scoring_text(row) -> str:
    """Text used for ranking only (FR-MDQ-05 / FR-MDQ-08).

    The heading path carries the terms that identify a section, so matching it
    is what lets a heading-only term reach its chunk. The repository path does
    the same for terms that only appear in a file name. Neither may reach the
    excerpt, which stays anchored to the body and its line range.
    """
    heading = row["heading_path"] or ""
    parts = [row["path"]]
    if heading:
        parts.extend([heading] * HEADING_WEIGHT)
    parts.append(row["text"])
    return "\n".join(parts)


def _budget_cost(hit: Hit) -> int:
    """Budget charged for one hit (FR-MDQ-07).

    Measured on the machine-readable line the caller actually receives, so the
    metadata that travels with every hit is paid for rather than ignored.
    """
    from . import tokens as _tokens

    return max(1, _tokens.count_tokens(json.dumps(hit.to_dict(), ensure_ascii=False)))


def search(conn, query: str, *, mode: str = "bm25",
           top_k: int = 5, max_tokens: int = 800,
           path_globs: list[str] | None = None,
           tags: list[str] | None = None,
           snippet_radius: int = 2,
           include_parent: bool = False,
           parent_depth: int = 0,
           expand_neighbors: int = 0,
           merge_parts: bool = False,
           engine: str = "auto",
           fusion_alpha: float | None = None,
           return_unit: str = "line",
           pageindex_tree_depth: int = 0) -> list[Hit]:
    """Run a search against the indexed chunks.

    mode: 'bm25' | 'grep'
    engine: 'auto' | 'bm25' | 'fts5'. 'auto' picks 'fts5' when the env var
        MDQ_FTS5 (or the deprecated alias HVE_MDQ_FTS5) is set to a truthy
        value and FTS5 is available on this DB; otherwise it uses the
        in-memory BM25 path. A requested FTS5 search also falls back to the
        in-memory path when the query cannot be expressed against the index
        (a trigram index cannot represent terms shorter than 3 characters).
    include_parent: legacy boolean. When True and ``parent_depth`` <= 0, a
        single ancestor is fetched (back-compat).
    parent_depth: when >0, fetch up to N ancestor heading chunks. Each hit's
        ``expansion['parent']`` becomes a single dict for depth=1, or a list
        of dicts (root-most last) for depth>=2.
    fusion_alpha: when not None and the index contains ``chunk_embedding``
        values (late-chunking, Q9=B), blends scores as
        ``final = alpha * bm25_norm + (1 - alpha) * cosine_sim``. Set to
        ``1.0`` to disable blending (BM25 only); ``0.0`` for cosine only.
        Ignored entirely when no rows have a non-NULL chunk_embedding.
    return_unit: 'line' (default) centres a ``snippet_radius`` window on the
        strongest matching line; 'chunk' returns the matching chunk body in
        full. The unit only widens the excerpt: ranking and the set of
        matching chunks are unchanged, though a fixed ``max_tokens`` budget
        admits fewer hits when excerpts are longer.
    pageindex_tree_depth: when >0 and the DB has any non-NULL ``summary``
        column (pageindex strategy), attach
        ``expansion['tree_path']`` to each hit: the ordered chain from up
        to N root-most ancestors (last) to the hit itself (first), each
        node = ``{chunk_id, heading_path, summary}``. 0 disables.
    """
    import os
    from . import store as _store

    # Normalise parent expansion arguments.
    if include_parent and parent_depth <= 0:
        parent_depth = 1
    if parent_depth > 0:
        include_parent = True

    # Resolve engine selection. fts5 only applies for bm25-mode queries.
    use_fts5 = False
    if mode == "bm25":
        if engine == "fts5":
            use_fts5 = True
        elif engine == "auto":
            _truthy = ("1", "true", "yes", "on")
            _new = os.environ.get("MDQ_FTS5", "").lower()
            _legacy = os.environ.get("HVE_MDQ_FTS5", "").lower()
            if _new in _truthy:
                use_fts5 = True
            elif _legacy in _truthy:
                use_fts5 = True
                import warnings as _warnings
                _warnings.warn(
                    "HVE_MDQ_FTS5 is deprecated; please use MDQ_FTS5 instead.",
                    DeprecationWarning,
                    stacklevel=2,
                )
        if use_fts5 and not _store.has_fts5(conn):
            use_fts5 = False  # silent fallback

    if use_fts5:
        hits_fts = _search_fts5(
            conn, query,
            top_k=top_k, max_tokens=max_tokens,
            path_globs=path_globs, tags=tags,
            snippet_radius=snippet_radius,
            include_parent=include_parent,
            parent_depth=parent_depth,
            expand_neighbors=expand_neighbors,
            merge_parts=merge_parts,
            return_unit=return_unit,
        )
        if hits_fts is not None:
            if pageindex_tree_depth > 0:
                _apply_tree_path(conn, hits_fts, pageindex_tree_depth)
            return hits_fts
        # The query cannot be expressed against this FTS5 index (e.g. shorter
        # than a trigram); fall through to the in-memory path.

    rows = _store.all_chunks(conn)
    if path_globs:
        rows = [r for r in rows if _path_matches(r["path"], path_globs)]
    if tags:
        rows = [r for r in rows if _tag_matches(r["tags"], tags)]

    if not rows:
        return []

    q_tokens = tokenize(query)

    if mode == "grep" or not q_tokens:
        pat = re.compile(re.escape(query), re.IGNORECASE)
        scored = []
        for r in rows:
            n = len(pat.findall(r["text"]))
            if n > 0:
                scored.append((float(n), r))
        _sort_scored(scored, query)
    else:
        corpus = [_tokenize.scoring_terms(_scoring_text(r)) for r in rows]
        q_terms = _tokenize.scoring_terms(query)
        if HAS_RANK_BM25:
            bm25 = BM25Okapi(corpus, b=LENGTH_NORM_B)
            scores = bm25.get_scores(q_terms)
        else:
            bm25 = _MiniBM25(corpus, b=LENGTH_NORM_B)
            scores = bm25.get_scores(q_terms)
        # Late-chunking fusion (Q9=B). Applies only when:
        #   1. The caller provided fusion_alpha (not None).
        #   2. The DB has at least one row with a non-NULL chunk_embedding.
        # We embed the query once and blend cosine_sim with min-max
        # normalised BM25.
        scores = _maybe_apply_fusion(rows, scores, query, fusion_alpha)
        scored = [(float(s), r) for s, r in zip(scores, rows) if s > 0]
        _sort_scored(scored, query)

    hits: list[Hit] = []
    spent = 0
    seen_ranges: set[tuple[str, int, int]] = set()  # overlap dedup
    for score, r in scored[: max(top_k * 6, top_k)]:
        key = (r["path"], int(r["start_line"]), int(r["end_line"]))
        if key in seen_ranges:
            continue
        seen_ranges.add(key)
        snippet = _excerpt(r["text"], q_tokens, snippet_radius, return_unit)
        candidate = Hit(
            chunk_id=r["chunk_id"],
            path=r["path"],
            heading_path=r["heading_path"],
            start_line=r["start_line"],
            end_line=r["end_line"],
            score=score,
            snippet=snippet,
        )
        est = _budget_cost(candidate)
        if spent + est > max_tokens and hits:
            break
        spent += est
        hits.append(candidate)
        if len(hits) >= top_k:
            break

    # T04: expansion (parent / neighbors / parts)
    _apply_expansion(conn, hits, include_parent, parent_depth,
                     expand_neighbors, merge_parts, return_unit)
    if pageindex_tree_depth > 0:
        _apply_tree_path(conn, hits, pageindex_tree_depth)
    return hits


def _apply_expansion(conn, hits: list[Hit], include_parent: bool,
                     parent_depth: int,
                     expand_neighbors: int, merge_parts: bool,
                     return_unit: str = "line") -> None:
    if not (include_parent or expand_neighbors > 0 or merge_parts):
        return
    for h in hits:
        exp: dict = {}
        if include_parent:
            depth = parent_depth if parent_depth > 0 else 1
            chain = _resolve_parent_chain(conn, h, depth)
            if chain:
                # Stable API contract (backward compatible):
                #   - ``expansion.parent``  : always a single dict (direct parent),
                #     or absent when no ancestor exists.
                #   - ``expansion.parents`` : present only when depth >= 2 and at
                #     least one ancestor beyond the direct parent was found.
                #     Ordered from direct parent (index 0) to root-most (last).
                # This avoids depth-dependent type variance on ``parent``.
                exp["parent"] = chain[0]
                if depth >= 2 and len(chain) >= 2:
                    exp["parents"] = chain
        if expand_neighbors > 0:
            neigh = _resolve_neighbors(conn, h, expand_neighbors)
            if neigh:
                exp["neighbors"] = neigh
        if merge_parts:
            parts = _resolve_parts(conn, h)
            if parts:
                exp["parts"] = parts
        if exp:
            h.expansion = _strip_bodies(exp) if return_unit == "locations" else exp


def _strip_bodies(expansion: dict) -> dict:
    """Drop the body from every expanded row (FR-MDQ-10 (6))."""
    stripped: dict = {}
    for key, value in expansion.items():
        rows = value if isinstance(value, list) else [value]
        cleaned = [{k: v for k, v in row.items() if k != "text"} for row in rows]
        stripped[key] = cleaned if isinstance(value, list) else cleaned[0]
    return stripped


def _build_fts5_query(q_tokens: list[str]) -> str:
    """Quote each token and OR-join for FTS5 MATCH."""
    safe = []
    for t in q_tokens:
        # double-quote and escape internal double-quotes per FTS5 syntax.
        safe.append('"' + t.replace('"', '""') + '"')
    return " OR ".join(safe)


def _fts5_tokenizer(conn) -> str:
    """Return the tokenizer declared on ``chunks_fts`` (empty when absent)."""
    import sqlite3 as _sql
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
        ).fetchone()
    except _sql.OperationalError:
        return ""
    m = re.search(r"tokenize='([^']+)'", (row[0] if row else "") or "")
    return m.group(1) if m else ""


def _build_trigram_query(query: str) -> tuple[str, list[str]]:
    """Return an FTS5 MATCH expression for a trigram index and its segments.

    ``detail=none`` rejects phrase queries, so each whitespace-delimited
    segment becomes the logical AND of its trigrams and segments are ORed.
    That admits documents holding the trigrams non-contiguously, so callers
    must confirm each segment with LIKE. Segments below the trigram minimum
    cannot be expressed at all and are dropped.
    """
    clauses: list[str] = []
    used: list[str] = []
    for seg in query.split():
        if len(seg) < _MIN_TRIGRAM_CHARS:
            continue
        grams = sorted({
            seg[i:i + _MIN_TRIGRAM_CHARS]
            for i in range(len(seg) - _MIN_TRIGRAM_CHARS + 1)
        })
        joined = " AND ".join('"' + g.replace('"', '""') + '"' for g in grams)
        clauses.append(f"({joined})")
        used.append(seg)
    return " OR ".join(clauses), used


def _like_pattern(segment: str) -> str:
    """Wrap a segment for LIKE, escaping the wildcards so it stays literal.

    Without this, ``_`` and ``%`` in the query widen the check that is meant
    to confirm the trigram candidates actually contain the segment.
    """
    escaped = (
        segment.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


def _search_fts5(conn, query: str, *, top_k: int, max_tokens: int,
                 path_globs: list[str] | None,
                 tags: list[str] | None,
                 snippet_radius: int,
                 include_parent: bool,
                 parent_depth: int,
                 expand_neighbors: int,
                 merge_parts: bool,
                 return_unit: str = "line") -> list[Hit] | None:
    """Run the FTS5 query, or return None when this index cannot serve it."""
    import sqlite3 as _sql
    conn.row_factory = _sql.Row
    q_tokens = tokenize(query)
    if not q_tokens:
        return []
    verify_sql = ""
    verify_params: list[str] = []
    if _fts5_tokenizer(conn) == "trigram":
        fts_q, segments = _build_trigram_query(query)
        if not fts_q:
            return None  # too short for the trigram index; caller falls back
        verify_sql = (
            " AND ("
            + " OR ".join(["c.text LIKE ? ESCAPE '\\'"] * len(segments))
            + ")"
        )
        verify_params = [_like_pattern(s) for s in segments]
    else:
        fts_q = _build_fts5_query(q_tokens)
    try:
        rows = list(conn.execute(
            "SELECT c.chunk_id, c.path, c.heading_path, c.level, "
            "c.start_line, c.end_line, c.token_est, c.text, c.tags, "
            "c.part_index, c.part_total, bm25(chunks_fts) AS bm "
            "FROM chunks c JOIN chunks_fts f ON f.rowid = c.rowid "
            "WHERE chunks_fts MATCH ?" + verify_sql + " "
            "ORDER BY bm ASC",
            (fts_q, *verify_params),
        ))
    except _sql.OperationalError:
        return []

    if path_globs:
        rows = [r for r in rows if _path_matches(r["path"], path_globs)]
    if tags:
        rows = [r for r in rows if _tag_matches(r["tags"], tags)]
    rows.sort(key=lambda row: (
        -_exact_identifier_tag_priority(row["tags"], query),
        float(row["bm"]),
        str(row["path"]),
        int(row["start_line"]),
        str(row["chunk_id"]),
    ))

    hits: list[Hit] = []
    spent = 0
    seen_ranges: set[tuple[str, int, int]] = set()
    for r in rows[: max(top_k * 6, top_k)]:
        key = (r["path"], int(r["start_line"]), int(r["end_line"]))
        if key in seen_ranges:
            continue
        seen_ranges.add(key)
        snippet = _excerpt(r["text"], q_tokens, snippet_radius, return_unit)
        # FTS5 bm25() returns negative values; smaller = better. Surface a
        # positive monotonic score for consumers (negate).
        candidate = Hit(
            chunk_id=r["chunk_id"],
            path=r["path"],
            heading_path=r["heading_path"],
            start_line=r["start_line"],
            end_line=r["end_line"],
            score=-float(r["bm"]),
            snippet=snippet,
        )
        est = _budget_cost(candidate)
        if spent + est > max_tokens and hits:
            break
        spent += est
        hits.append(candidate)
        if len(hits) >= top_k:
            break
    _apply_expansion(conn, hits, include_parent, parent_depth,
                     expand_neighbors, merge_parts, return_unit)
    return hits


def _row_to_brief(row) -> dict:
    return {
        "chunk_id": row["chunk_id"],
        "path": row["path"],
        "heading_path": row["heading_path"],
        "lines": [row["start_line"], row["end_line"]],
        "text": row["text"],
    }


def _resolve_parent(conn, hit: Hit) -> dict | None:
    """Resolve the immediate parent (legacy single-depth API).

    Thin wrapper over :func:`_resolve_parent_chain` for any external caller
    still importing this symbol. Internally :func:`_apply_expansion` calls
    ``_resolve_parent_chain`` directly.
    """
    chain = _resolve_parent_chain(conn, hit, 1)
    return chain[0] if chain else None


def _resolve_parent_chain(conn, hit: Hit, depth: int) -> list[dict]:
    """Walk up to ``depth`` ancestor heading chunks.

    Returns the chain ordered from direct parent (index 0) to root-most
    ancestor (last). Stops early when no further ancestor exists.
    """
    import sqlite3 as _sql
    conn.row_factory = _sql.Row
    chain: list[dict] = []
    current_chunk_id = hit.chunk_id
    current_hp = hit.heading_path or ""
    seen: set[str] = set()
    for _ in range(max(0, depth)):
        parent_dict = None
        # Prefer parent_chunk_id column when available.
        try:
            row = conn.execute(
                "SELECT parent_chunk_id FROM chunks WHERE chunk_id = ?",
                (current_chunk_id,),
            ).fetchone()
        except Exception:
            row = None
        parent_id = row["parent_chunk_id"] if row else None
        if parent_id and parent_id not in seen:
            prow = conn.execute(
                "SELECT chunk_id, path, heading_path, start_line, end_line, text "
                "FROM chunks WHERE chunk_id = ?",
                (parent_id,),
            ).fetchone()
            if prow:
                parent_dict = _row_to_brief(prow)
                current_chunk_id = prow["chunk_id"]
                current_hp = prow["heading_path"]
        if parent_dict is None:
            # Fallback path.
            if " > " not in current_hp:
                break
            parent_hp = current_hp.rsplit(" > ", 1)[0]
            prow = conn.execute(
                "SELECT chunk_id, path, heading_path, start_line, end_line, text "
                "FROM chunks WHERE path = ? AND heading_path = ? "
                "ORDER BY start_line LIMIT 1",
                (hit.path, parent_hp),
            ).fetchone()
            if not prow:
                break
            parent_dict = _row_to_brief(prow)
            current_chunk_id = prow["chunk_id"]
            current_hp = prow["heading_path"]
        if current_chunk_id in seen:
            break
        seen.add(current_chunk_id)
        chain.append(parent_dict)
    return chain


def _apply_tree_path(conn, hits: list[Hit], depth: int) -> None:
    """Attach ``expansion['tree_path']`` for pageindex-style navigation.

    For each hit, fetches up to ``depth`` ancestor heading chunks plus the
    hit chunk itself, projecting ``(chunk_id, heading_path, summary)``.
    The returned chain is ordered root-most first, hit last (i.e. natural
    table-of-contents order). Only populates when the DB has a non-NULL
    ``summary`` column on at least one node in the chain (pageindex
    strategy); otherwise leaves ``expansion['tree_path']`` absent.
    """
    if depth <= 0 or not hits:
        return
    import sqlite3 as _sql
    conn.row_factory = _sql.Row
    # Detect whether the schema even has the summary column. SCHEMA v6+.
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
    except Exception:
        return
    if "summary" not in cols:
        return
    for h in hits:
        # Reuse the parent-chain walker, then build a root-first chain
        # with the hit node appended last.
        chain_parents = _resolve_parent_chain(conn, h, depth)
        # _resolve_parent_chain returns direct-parent first, root-most last.
        # Reverse to get root-first.
        ordered = list(reversed(chain_parents))
        # Fetch summaries for the parent chunks (a single IN query).
        ids = [n.get("chunk_id") for n in ordered if n.get("chunk_id")]
        summaries: dict[str, str | None] = {}
        if ids:
            placeholders = ",".join("?" * len(ids))
            try:
                for row in conn.execute(
                    f"SELECT chunk_id, summary FROM chunks "
                    f"WHERE chunk_id IN ({placeholders})",
                    ids,
                ):
                    summaries[row["chunk_id"]] = row["summary"]
            except Exception:
                summaries = {}
        # Also fetch the hit's own summary.
        hit_summary: str | None = None
        try:
            row = conn.execute(
                "SELECT summary FROM chunks WHERE chunk_id = ?",
                (h.chunk_id,),
            ).fetchone()
            hit_summary = row["summary"] if row else None
        except Exception:
            pass
        # Build the projected nodes.
        nodes: list[dict] = []
        for n in ordered:
            nodes.append({
                "chunk_id": n.get("chunk_id"),
                "heading_path": n.get("heading_path"),
                "summary": summaries.get(n.get("chunk_id")),
            })
        nodes.append({
            "chunk_id": h.chunk_id,
            "heading_path": h.heading_path,
            "summary": hit_summary,
        })
        # Skip when no node in the chain has a summary (non-pageindex DB).
        if not any(n.get("summary") for n in nodes):
            continue
        if h.expansion is None:
            h.expansion = {}
        h.expansion["tree_path"] = nodes


def _resolve_neighbors(conn, hit: Hit, n: int) -> list[dict]:
    import sqlite3 as _sql
    conn.row_factory = _sql.Row
    before = list(conn.execute(
        "SELECT chunk_id, path, heading_path, start_line, end_line, text "
        "FROM chunks WHERE path = ? AND start_line < ? "
        "ORDER BY start_line DESC LIMIT ?",
        (hit.path, hit.start_line, n),
    ))
    after = list(conn.execute(
        "SELECT chunk_id, path, heading_path, start_line, end_line, text "
        "FROM chunks WHERE path = ? AND start_line > ? "
        "ORDER BY start_line ASC LIMIT ?",
        (hit.path, hit.start_line, n),
    ))
    out = [_row_to_brief(r) for r in reversed(before)]
    out.extend(_row_to_brief(r) for r in after)
    return out


def _resolve_parts(conn, hit: Hit) -> list[dict]:
    import sqlite3 as _sql
    conn.row_factory = _sql.Row
    rows = list(conn.execute(
        "SELECT chunk_id, path, heading_path, start_line, end_line, text, "
        "part_index, part_total FROM chunks "
        "WHERE path = ? AND heading_path = ? AND chunk_id != ? "
        "ORDER BY part_index",
        (hit.path, hit.heading_path, hit.chunk_id),
    ))
    # only include if siblings exist (part_total > 1)
    rows = [r for r in rows if (r["part_total"] or 1) > 1]
    return [_row_to_brief(r) for r in rows]


def get_chunk(conn, chunk_id: str) -> dict | None:
    from . import store as _store  # noqa: F401
    conn.row_factory = __import__("sqlite3").Row
    row = conn.execute(
        "SELECT chunk_id, path, heading_path, level, start_line, end_line, "
        "token_est, text, tags FROM chunks WHERE chunk_id = ?",
        (chunk_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "chunk_id": row["chunk_id"],
        "path": row["path"],
        "heading_path": row["heading_path"],
        "level": row["level"],
        "lines": [row["start_line"], row["end_line"]],
        "token_est": row["token_est"],
        "text": row["text"],
    }


def list_chunks(conn, path_globs: list[str] | None = None,
                heading_level: int | None = None,
                limit: int = 200) -> list[dict]:
    from . import store as _store  # noqa: F401
    conn.row_factory = __import__("sqlite3").Row
    rows = list(conn.execute(
        "SELECT path, heading_path, level, start_line, end_line FROM chunks "
        "ORDER BY path, start_line"
    ))
    out = []
    for r in rows:
        if path_globs and not _path_matches(r["path"], path_globs):
            continue
        if heading_level is not None and r["level"] != heading_level:
            continue
        out.append({
            "path": r["path"],
            "heading_path": r["heading_path"],
            "level": r["level"],
            "lines": [r["start_line"], r["end_line"]],
        })
        if len(out) >= limit:
            break
    return out
