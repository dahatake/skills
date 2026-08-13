"""Token-budgeted repository map (FR-CQ-09).

Follows Aider's repo map idea: show files with their most-referenced definition
lines only, ranked by how often the rest of the codebase refers to them, and cut
the tail so that the whole map fits an explicit token budget.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from cq import discovery, store, tokens

DEFAULT_MAX_TOKENS = 1200
FOLD_MARKER = "⋮..."


@dataclass(frozen=True)
class MapEntry:
    path: str
    name: str
    kind: str
    line: int
    signature: str
    callers: int
    score: float


@dataclass(frozen=True)
class RepoMap:
    entries: tuple[MapEntry, ...]
    dropped: int
    tokens: int


def _test_path(column: str) -> str:
    return f"{discovery.test_path_sql(column)} = 1"


# 同名シンボルが多いほど 1 件あたりの参照は「その名前を指した」証拠として弱い。
# `get` / `run` のような汎用名が定義数で割られ、名前衝突による偽の上位を防ぐ。
_RANK_SQL = f"""
SELECT s.path, s.name, s.kind, s.start_line, s.signature,
       (SELECT count(*) FROM refs r
         WHERE r.name = s.name AND r.path <> s.path
           AND NOT {_test_path('r.path')}) AS callers,
       (SELECT count(*) FROM symbols d
         WHERE d.name = s.name AND d.is_test = 0
           AND NOT {_test_path('d.path')}) AS definitions
FROM symbols s
WHERE s.is_test = 0 AND NOT {_test_path('s.path')}
"""


def build(
    db_path: Path,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    paths: str | None = None,
) -> RepoMap:
    clause, params = (" AND s.path GLOB ?", [paths]) if paths else ("", [])
    with closing(store.open_store(db_path, create=False)) as conn:
        rows = conn.execute(_RANK_SQL + clause, params).fetchall()

    ranked = [
        MapEntry(
            r["path"], r["name"], r["kind"], r["start_line"], r["signature"],
            int(r["callers"]), int(r["callers"]) / max(1, int(r["definitions"])),
        )
        for r in rows
    ]
    ranked.sort(key=lambda e: (-e.score, e.path, e.line))
    kept: list[MapEntry] = []
    used = 0
    for entry in ranked:
        cost = tokens.count_tokens(f"{FOLD_MARKER}\n{_line(entry)}")
        if not kept or entry.path != kept[-1].path:
            cost += tokens.count_tokens(f"{entry.path}:\n{FOLD_MARKER}\n")
        if kept and used + cost > max_tokens:
            break
        kept.append(entry)
        used += cost
    # トークナイズは連結に対して加法的でなく、見出し行の共有も上の見積りでは捉えきれない。
    # FR-CQ-09 は「実際に出力する文字列の全体」での判定を求めるので、描画結果を実測して詰める。
    while len(kept) > 1 and tokens.count_tokens(
        _render(kept, len(ranked) - len(kept))
    ) > max_tokens:
        kept.pop()
    total = tokens.count_tokens(_render(kept, len(ranked) - len(kept)))
    return RepoMap(tuple(kept), len(ranked) - len(kept), total)


def render(repo_map: RepoMap) -> str:
    """Group by file, print definition lines only — never bodies."""
    return _render(repo_map.entries, repo_map.dropped)


def _render(entries: Sequence[MapEntry], dropped: int) -> str:
    lines: list[str] = []
    by_path: dict[str, list[MapEntry]] = {}
    for entry in entries:
        by_path.setdefault(entry.path, []).append(entry)
    for path in sorted(by_path):
        lines.append(f"{path}:")
        for entry in sorted(by_path[path], key=lambda e: e.line):
            lines.append(FOLD_MARKER)
            lines.append(f"│{entry.signature}  # callers={entry.callers} L{entry.line}")
        lines.append(FOLD_MARKER)
        lines.append("")
    if dropped:
        lines.append(f"# dropped {dropped} lower-ranked symbols to fit the token budget")
    return "\n".join(lines)


def to_dict(repo_map: RepoMap) -> dict[str, object]:
    return {
        "tokens": repo_map.tokens,
        "dropped": repo_map.dropped,
        "entries": [
            {
                "path": e.path, "name": e.name, "kind": e.kind, "line": e.line,
                "signature": e.signature, "callers": e.callers, "score": round(e.score, 3),
            }
            for e in repo_map.entries
        ],
    }


def _line(entry: MapEntry) -> str:
    return f"│{entry.signature}  # callers={entry.callers} L{entry.line}"
