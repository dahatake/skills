"""Search over the cq index with rule-based query routing (FR-CQ-06).

Ranking never leaves SQLite: every layer returns at most ``top_k`` rows through
``ORDER BY rank`` or an indexed lookup, so the process never materialises the
whole index (NFR-CQ-01).
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence, TypedDict

from cq import store

ROUTES = ("trace", "symbol", "substr", "regex", "bm25")
MIN_SUBSTRING_CHARS = 3
DEFAULT_TOP_K = 5
DEFAULT_MAX_TOKENS = 800
DEFAULT_SNIPPET_RADIUS = 2
DEFAULT_REGEX_MAX_CANDIDATES = 500
# 索引済みファイルの stat 突合だけなら 1 クエリあたり数 ms で済む（FR-CQ-08）。
DEFAULT_AUTO_REINDEX_LIMIT = 50

_TRACE_RE = re.compile(r"^(?:(?:FR|NFR|UT|TEST)-[A-Z0-9]+(?:-[A-Z0-9]+)*|APP-\d{3}|SVC-\d{2}|UC-\d+)$")
_SYMBOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_BAREWORD_RE = re.compile(r"[^\W_]+[\w$]*|[\w$]+")
_PLAIN_RE = re.compile(r"^[A-Za-z0-9_ ]+$")
# 仮名・漢字・ハングル・全角記号。連言を選言へ緩和しない判定に使う（FR-CQ-06）。
_CJK_RE = re.compile(
    r"[\u1100-\u11ff\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff"
    r"\uac00-\ud7af\uf900-\ufaff\uff65-\uff9f]"
)
# BM25 の重み: 名前 > シグネチャ > 識別子展開 > 本文
_BM25_WEIGHTS = "10.0, 5.0, 3.0, 1.0"


class SearchError(RuntimeError):
    """Raised for unusable queries or a missing index."""


_LAST_STALENESS: ContextVar[dict[str, Any] | None] = ContextVar(
    "cq_last_staleness", default=None
)


def last_staleness() -> dict[str, Any] | None:
    """Staleness warning produced by the most recent :func:`search` call."""
    return _LAST_STALENESS.get()


class ChunkPayload(TypedDict):
    chunk_id: str
    path: str
    lines: list[int]
    text: str
    parser: str


def get_chunk(db_path: Path, chunk_id: str) -> ChunkPayload | None:
    """Return one indexed chunk with its parser provenance (FR-CQ-13)."""
    with closing(store.open_store(db_path, create=False)) as conn:
        row = conn.execute(
            "SELECT c.chunk_id, c.path, c.start_line, c.end_line, c.text, f.parser "
            "FROM chunks c JOIN files f ON f.path = c.path "
            "WHERE c.chunk_id = ?",
            (chunk_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "chunk_id": row["chunk_id"],
        "path": row["path"],
        "lines": [row["start_line"], row["end_line"]],
        "text": row["text"],
        "parser": row["parser"],
    }


@dataclass
class Hit:
    path: str
    lines: list[int]
    route: str
    score: float
    snippet: str
    parser: str
    chunk_id: str | None = None
    qualname: str | None = None
    kind: str | None = None
    signature: str | None = None
    truncated: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "path": self.path,
            "lines": self.lines,
            "route": self.route,
            "score": round(self.score, 4),
            "snippet": self.snippet,
            "parser": self.parser,
        }
        for key in ("chunk_id", "qualname", "kind", "signature"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        if self.truncated:
            payload["truncated"] = True
        payload.update(self.extra)
        return payload


def sanitize_match(query: str) -> str:
    """Reduce a free-form query to FTS5 barewords joined by implicit AND.

    Quotes, parentheses and operators are dropped: `chunks_fts` is created with
    ``detail=column``, where a phrase query raises ``OperationalError``.
    """
    words = _BAREWORD_RE.findall(query)
    return " ".join(w for w in words if w.upper() not in {"AND", "OR", "NOT", "NEAR"})


def choose_route(query: str | None, regex: str | None) -> str:
    if regex is not None:
        return "regex"
    text = (query or "").strip()
    if _TRACE_RE.match(text):
        return "trace"
    if _SYMBOL_RE.match(text):
        return "symbol"
    if not _PLAIN_RE.match(text) and len(text) >= MIN_SUBSTRING_CHARS:
        return "substr"
    return "bm25"


def search(
    repo_root: Path,
    profile: str,
    *,
    query: str | None = None,
    regex: str | None = None,
    mode: str = "auto",
    top_k: int = DEFAULT_TOP_K,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    snippet_radius: int = DEFAULT_SNIPPET_RADIUS,
    regex_max_candidates: int = DEFAULT_REGEX_MAX_CANDIDATES,
    paths: str | None = None,
    return_unit: str = "line",
    db_path: Path | None = None,
    auto_reindex_limit: int | None = DEFAULT_AUTO_REINDEX_LIMIT,
) -> list[Hit]:
    _LAST_STALENESS.set(None)
    if query is None and regex is None:
        raise SearchError("either a query or a regex is required")
    if mode != "auto" and mode not in ROUTES:
        raise SearchError(f"unknown mode: {mode!r}; expected auto or one of {ROUTES}")

    target = Path(db_path) if db_path else repo_root / store.db_path_for(profile)
    _guard_freshness(repo_root, profile, target, auto_reindex_limit)
    route = mode if mode != "auto" else choose_route(query, regex)
    order = [route] if mode != "auto" else _fallback_order(route)

    with closing(store.open_store(target, create=False)) as conn:
        for candidate in order:
            hits = _run(conn, candidate, query, regex, top_k, snippet_radius,
                        regex_max_candidates, paths, explicit=mode != "auto")
            if hits:
                if return_unit == "chunk":
                    _widen_to_chunk_bodies(conn, hits)
                return _cap_tokens(hits, max_tokens)
    return []


def _widen_to_chunk_bodies(conn, hits: Sequence[Hit]) -> None:
    """Replace each excerpt with its whole chunk body (FR-CQ-06).

    Applied after routing so that the unit never influences which chunks
    match or how they rank, and before the token budget so the budget is
    computed on what is actually returned. ``lines`` is widened together with
    the text: routes such as regex report only the matching line, which would
    otherwise contradict the excerpt.
    """
    for hit in hits:
        if not hit.chunk_id:
            continue
        row = conn.execute(
            "SELECT text, start_line, end_line FROM chunks WHERE chunk_id = ?",
            (hit.chunk_id,),
        ).fetchone()
        if row is not None:
            hit.snippet = row["text"]
            hit.lines = [row["start_line"], row["end_line"]]


def _guard_freshness(
    repo_root: Path, profile: str, db_path: Path, limit: int | None
) -> None:
    """Re-index small drifts before answering; report larger ones as stale.

    Deliberately imported here so that a plain search never pays for the
    discovery / config modules when freshness checking is switched off.
    """
    global _LAST_STALENESS
    if limit is None or limit < 0:
        return
    from cq import config, freshness

    try:
        resolved = config.resolve_profile(repo_root, profile)
        report = freshness.check(repo_root, resolved, db_path)
    except Exception:
        return  # 鮮度確認は補助機構。失敗しても検索そのものは行う。
    if report.is_fresh:
        return
    if len(report.changed) <= limit:
        freshness.refresh(repo_root, resolved, db_path, report.changed)
        return
    _LAST_STALENESS.set({
        "warning": "stale",
        "changed": len(report.changed),
        "hint": "run `python -m cq index` to refresh the index",
    })


def _fallback_order(route: str) -> list[str]:
    # `path` は連鎖の末尾だけに置く。`ROUTES` へは入れないので `--mode path` は選べない。
    chains = {
        "symbol": ["symbol", "substr", "bm25", "path"],
        "substr": ["substr", "bm25", "symbol", "path"],
        "bm25": ["bm25", "substr", "symbol", "path"],
        "trace": ["trace", "symbol", "substr", "bm25", "path"],
        "regex": ["regex"],
    }
    return chains[route]


def _run(conn, route, query, regex, top_k, radius, regex_max, paths, *, explicit) -> list[Hit]:
    if route == "regex":
        return _regex(conn, regex, top_k, radius, regex_max, paths)
    if route == "trace":
        return _trace(conn, query, top_k, radius, paths)
    if route == "symbol":
        return _symbol(conn, query, top_k, radius, paths)
    if route == "substr":
        return _substr(conn, query, top_k, radius, paths, explicit=explicit)
    if route == "path":
        return _path(conn, query, top_k, radius, paths)
    return _bm25(conn, query, top_k, radius, paths)


def _path_clause(paths: str | None, column: str) -> tuple[str, list[Any]]:
    return (f" AND {column} GLOB ?", [paths]) if paths else ("", [])


def _test_rank(column: str) -> str:
    """1 for test code, 0 otherwise: implementations outrank their tests."""
    from cq import discovery

    return discovery.test_path_sql(column)


def _symbol(conn, query, top_k, radius, paths) -> list[Hit]:
    text = (query or "").strip()
    clause, params = _path_clause(paths, "s.path")
    sql = (
        "SELECT s.path, s.start_line, s.end_line, s.qualname, s.kind, s.signature,"
        " f.parser FROM symbols s JOIN files f ON f.path = s.path"
        " WHERE (s.qualname = ? OR s.name = ?)" + clause +
        f" ORDER BY {_test_rank('s.path')}, length(s.qualname), s.path, s.start_line LIMIT ?"
    )
    rows = conn.execute(sql, [text, text, *params, top_k]).fetchall()
    exact = True
    if not rows and "." in text:
        # `module.symbol` 形式: 末尾セグメントで再探索し、前方セグメントをファイル名ヒントに使う。
        # 完全一致ではないため、呼び出し側が誤認しないよう score と match で明示する。
        exact = False
        head, _, tail = text.rpartition(".")
        rows = conn.execute(
            "SELECT s.path, s.start_line, s.end_line, s.qualname, s.kind, s.signature,"
            " f.parser FROM symbols s JOIN files f ON f.path = s.path"
            " WHERE s.name = ?" + clause +
            f" ORDER BY CASE WHEN s.path GLOB ? THEN 0 ELSE 1 END, {_test_rank('s.path')},"
            " length(s.qualname), s.path, s.start_line LIMIT ?",
            [tail, *params, f"*{head.rpartition('.')[2]}.*", top_k],
        ).fetchall()
    hits = []
    for r in rows:
        snippet, chunk_id = _file_snippet(conn, r["path"], r["start_line"], radius)
        hits.append(Hit(
            path=r["path"], lines=[r["start_line"], r["end_line"]], route="symbol",
            score=1.0 if exact else 0.5, snippet=snippet, parser=r["parser"],
            chunk_id=chunk_id, qualname=r["qualname"], kind=r["kind"],
            signature=r["signature"],
            extra={"match": "qualname" if exact else "name-fallback"},
        ))
    return hits


def _trace(conn, query, top_k, radius, paths) -> list[Hit]:
    clause, params = _path_clause(paths, "t.path")
    rows = conn.execute(
        "SELECT t.path, t.line, t.trace_id, t.doc_path, t.anchor, f.parser"
        " FROM traces t JOIN files f ON f.path = t.path WHERE t.trace_id = ?" + clause +
        # 出典参照を持つ行を優先し、テストコードは後ろへ回す。
        " ORDER BY CASE WHEN t.doc_path IS NULL THEN 1 ELSE 0 END,"
        f" {_test_rank('t.path')}, t.path, t.line LIMIT ?",
        [(query or "").strip(), *params, top_k],
    ).fetchall()
    hits = []
    for r in rows:
        snippet, chunk_id = _file_snippet(conn, r["path"], r["line"], radius)
        hits.append(Hit(
            path=r["path"], lines=[r["line"], r["line"]], route="trace", score=1.0,
            snippet=snippet, parser=r["parser"], chunk_id=chunk_id,
            extra={"trace_id": r["trace_id"], "doc_path": r["doc_path"], "anchor": r["anchor"]},
        ))
    return hits


def _substr(conn, query, top_k, radius, paths, *, explicit: bool = True) -> list[Hit]:
    text = (query or "").strip().strip('"')
    if len(text) < MIN_SUBSTRING_CHARS:
        if not explicit:
            return []  # auto ルーティングの fallback では連鎖を止めない
        raise SearchError(
            f"substring queries need at least {MIN_SUBSTRING_CHARS} characters"
            " (the trigram index cannot be used below that and would scan everything)"
        )
    clause, params = _path_clause(paths, "c.path")
    rows = conn.execute(
        "SELECT c.chunk_id, c.path, c.start_line, c.end_line, c.text, c.name,"
        " c.signature, f.parser FROM chunks_tri t JOIN chunks c ON c.rowid = t.rowid"
        " JOIN files f ON f.path = c.path WHERE t.text LIKE ?" + clause +
        # 行頭に現れる語は定義・代入であることが多いので先に出す。
        " ORDER BY CASE WHEN c.text LIKE ? OR c.text LIKE ? THEN 0 ELSE 1 END,"
        f" {_test_rank('c.path')}, c.path, c.start_line LIMIT ?",
        [f"%{text}%", *params, f"{text}%", f"%{chr(10)}{text}%", top_k],
    ).fetchall()
    return [_chunk_hit(r, "substr", 1.0, text, radius) for r in rows]


def _path(conn, query, top_k, radius, paths) -> list[Hit]:
    """Match the repository-relative path itself (FR-CQ-06, last resort).

    `chunks_fts` indexes the body, the name, the signature and the split
    identifiers, but never the path, so a test module name coming out of a
    stack trace is otherwise unreachable. One hit per file: `min(start_line)`
    makes SQLite take the remaining bare columns from that same row.
    """
    text = (query or "").strip()
    if len(text) < MIN_SUBSTRING_CHARS:
        return []  # trigram 層と同じ最小長。連鎖の末尾なのでエラーにはしない。
    clause, params = _path_clause(paths, "f.path")
    # 絞り込みは `files`（実測 821 行）側で行う。`chunks`（14,055 行）を走査すると
    # 同じ結果に 6〜17 倍の時間がかかる。
    rows = conn.execute(
        "SELECT c.chunk_id, c.path, min(c.start_line) AS start_line, c.end_line,"
        " c.text, c.name, c.signature, f.parser"
        " FROM files f JOIN chunks c ON c.path = f.path"
        " WHERE f.path LIKE ?" + clause +
        f" GROUP BY f.path ORDER BY {_test_rank('f.path')}, length(f.path), f.path"
        " LIMIT ?",
        [f"%{text}%", *params, top_k],
    ).fetchall()
    return [_chunk_hit(r, "path", 1.0, text, radius) for r in rows]


def _bm25(conn, query, top_k, radius, paths) -> list[Hit]:
    match = sanitize_match(query or "")
    if not match:
        return []
    terms = match.split()
    rows = _bm25_rows(conn, match, top_k, paths)
    if rows:
        return [_chunk_hit(r, "bm25", -float(r["score"]), terms[0], radius) for r in rows]
    # 連言が 0 件のときだけ選言へ 1 回緩和する。CJK を含むクエリは、誤った上位ヒットを
    # 返すより 0 件を返す既存方針を維持するため対象外にする（FR-CQ-06）。
    if len(terms) < 2 or _CJK_RE.search(match):
        return []
    rows = _bm25_rows(conn, " OR ".join(terms), top_k, paths)
    return [
        _chunk_hit(r, "bm25", -float(r["score"]), terms[0], radius,
                   extra={"match": "or-fallback"})
        for r in rows
    ]


def _bm25_rows(conn, match: str, top_k: int, paths):
    clause, params = _path_clause(paths, "c.path")
    try:
        return conn.execute(
            "SELECT c.chunk_id, c.path, c.start_line, c.end_line, c.text, c.name,"
            f" c.signature, f.parser, bm25(chunks_fts, {_BM25_WEIGHTS}) AS score"
            " FROM chunks_fts f2 JOIN chunks c ON c.rowid = f2.rowid"
            " JOIN files f ON f.path = c.path WHERE chunks_fts MATCH ?" + clause +
            " ORDER BY rank LIMIT ?",
            [match, *params, top_k],
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def _regex(conn, regex, top_k, radius, regex_max, paths) -> list[Hit]:
    try:
        pattern = re.compile(regex, re.MULTILINE)
    except re.error as exc:
        raise SearchError(f"invalid regex: {exc}") from exc

    literal = _longest_literal(regex)
    clause, params = _path_clause(paths, "c.path")
    if literal and len(literal) >= MIN_SUBSTRING_CHARS:
        sql = ("SELECT c.chunk_id, c.path, c.start_line, c.end_line, c.text, c.name,"
               " c.signature, f.parser FROM chunks_tri t JOIN chunks c ON c.rowid = t.rowid"
               " JOIN files f ON f.path = c.path WHERE t.text LIKE ?" + clause +
               " ORDER BY c.path, c.start_line LIMIT ?")
        args = [f"%{literal}%", *params, regex_max + 1]
    else:
        sql = ("SELECT c.chunk_id, c.path, c.start_line, c.end_line, c.text, c.name,"
               " c.signature, f.parser FROM chunks c JOIN files f ON f.path = c.path"
               " WHERE 1=1" + clause + " ORDER BY c.path, c.start_line LIMIT ?")
        args = [*params, regex_max + 1]

    candidates = conn.execute(sql, args).fetchall()
    truncated = len(candidates) > regex_max
    hits: list[Hit] = []
    for row in candidates[:regex_max]:
        for offset, line in enumerate(row["text"].splitlines()):
            if pattern.search(line):
                number = row["start_line"] + offset
                hits.append(Hit(
                    path=row["path"], lines=[number, number], route="regex", score=1.0,
                    snippet=_window(row["text"], row["start_line"], number, radius),
                    parser=row["parser"], chunk_id=row["chunk_id"],
                    signature=row["signature"] or None, truncated=truncated,
                ))
                break
        if len(hits) >= top_k:
            break
    return hits


def _longest_literal(regex: str) -> str:
    """Longest metacharacter-free run, used to pre-filter with the trigram index."""
    return max(re.split(r"[\\^$.|?*+()\[\]{}]+", regex), key=len, default="")


def _chunk_hit(row, route: str, score: float, needle: str, radius: int,
               extra: dict[str, Any] | None = None) -> Hit:
    number = _best_line(row["text"], row["start_line"], needle)
    return Hit(
        path=row["path"], lines=[row["start_line"], row["end_line"]], route=route,
        score=score, snippet=_window(row["text"], row["start_line"], number, radius),
        parser=row["parser"], chunk_id=row["chunk_id"],
        signature=row["signature"] or None, extra=dict(extra or {}),
    )


def _best_line(text: str, start_line: int, needle: str) -> int:
    lowered = needle.lower()
    for offset, line in enumerate(text.splitlines()):
        if lowered and lowered in line.lower():
            return start_line + offset
    return start_line


def _window(text: str, start_line: int, focus: int, radius: int) -> str:
    lines = text.splitlines()
    index = max(0, focus - start_line)
    lo = max(0, index - radius)
    hi = min(len(lines), index + radius + 1)
    return "\n".join(lines[lo:hi])


def _file_snippet(conn, path: str, line: int, radius: int) -> tuple[str, str | None]:
    row = conn.execute(
        "SELECT chunk_id, start_line, text FROM chunks WHERE path = ? AND start_line <= ?"
        " AND end_line >= ? LIMIT 1", (path, line, line)
    ).fetchone()
    if row is None:
        return "", None
    return _window(row["text"], row["start_line"], line, radius), row["chunk_id"]


def _cap_tokens(hits: Sequence[Hit], max_tokens: int) -> list[Hit]:
    kept: list[Hit] = []
    used = 0
    for hit in hits:
        # 予算は抜粋だけでなく「実際に返す 1 ヒット分の機械可読表現の全体」で見積もる
        # （NFR-CQ-01）。CLI は `ensure_ascii=True` で出力するのでその形で数える。
        cost = max(1, len(json.dumps(hit.to_dict(), ensure_ascii=True)) // 4)
        if kept and used + cost > max_tokens:
            break
        kept.append(hit)
        used += cost
    return kept
