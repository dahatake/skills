"""HVE Tool Search — ランキング（FR-TS-04）。

フィールド重み付き BM25。日本語クエリで機能させるため、トークナイズは
``mdq.tokenize.scoring_terms``（CJK 連続を隣接バイグラムへ分割）を再利用し、
識別子（``snake_case`` / ``camelCase`` / ``mcp__azure__foo``）を部分語へ展開する。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from mdq.tokenize import scoring_terms

from .types import ToolEntry

# mdq と同じ文書長正規化係数。ツール定義は文書長のばらつきが大きいため弱めに効かせる。
LENGTH_NORM_B = 0.2

# 索引するフィールドの順序（policy.json の field_weights のキーと一致させる）。
FIELD_ORDER: tuple[str, ...] = ("name", "additional_search_text", "description", "arg_terms")

_ASCII_TOKEN_RE = re.compile(r"^[a-z0-9_]+$")
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def split_identifier(token: str) -> list[str]:
    """``snake_case`` / ``camelCase`` / ``mcp__azure__foo`` を部分語へ分解する。"""
    parts: list[str] = []
    for chunk in _CAMEL_BOUNDARY_RE.sub(" ", token).replace("_", " ").split():
        lowered = chunk.lower()
        if lowered and lowered not in parts:
            parts.append(lowered)
    return parts


def tokenize(text: str) -> list[str]:
    """検索・索引で共通に使うトークン列を返す。"""
    if not text:
        return []
    terms = list(scoring_terms(text))
    extra: list[str] = []
    for term in terms:
        if not _ASCII_TOKEN_RE.match(term):
            continue
        for part in split_identifier(term):
            if part != term:
                extra.append(part)
    return terms + extra


def _field_text(entry: ToolEntry, field: str) -> str:
    if field == "arg_terms":
        return " ".join(entry.arg_terms)
    return str(getattr(entry, field, "") or "")


def resolve_bm25_engine(prefer: str | None = None) -> tuple[str, Any]:
    """BM25 実装を選ぶ。既定は mdq の ``_MiniBM25``。

    ``rank_bm25.BM25Okapi`` を既定にしないのは、その idf が
    ``log(N - n + 0.5) - log(n + 0.5)`` であり、**ある語が全文書の半数に現れると idf が 0**
    になるため（実測: 2 件カタログで片方にだけ現れる語のスコアが 0）。
    HVE のカタログは数十〜数百件でこの退化が実際に起きる。
    ``_MiniBM25`` は ``log(1 + (N - n + 0.5) / (n + 0.5))`` で常に正の idf を返す。

    速度差は本件の規模では問題にならない。``prefer="rank_bm25"`` で明示的に切り替えられる。

    どちらも ``factory(corpus, b=...)`` → ``.get_scores(query) -> list[float]`` の同一 API。
    """
    if prefer == "rank_bm25":
        try:
            from rank_bm25 import BM25Okapi  # type: ignore[import-not-found]

            return "rank_bm25", BM25Okapi
        except ImportError:
            pass
    # mdq に同梱の stdlib 実装。private だが同一リポジトリ内なので API を固定できる。
    from mdq.search import _MiniBM25

    return "mini_bm25", _MiniBM25


@dataclass(frozen=True)
class RankedTool:
    entry: ToolEntry
    score: float


class ToolRanker:
    """`ToolEntry` 列に対するフィールド重み付き BM25 検索。"""

    def __init__(
        self,
        entries: Sequence[ToolEntry],
        field_weights: Mapping[str, float],
        *,
        tau: float = 0.4,
        engine: str | None = None,
    ) -> None:
        self.entries = tuple(entries)
        self.field_weights = {f: float(field_weights.get(f, 0.0)) for f in FIELD_ORDER}
        self.tau = float(tau)
        self.engine_name, factory = resolve_bm25_engine(engine)
        self._indexes: dict[str, Any] = {}
        if not self.entries:
            return
        for field in FIELD_ORDER:
            if self.field_weights[field] <= 0.0:
                continue
            corpus = [tokenize(_field_text(entry, field)) for entry in self.entries]
            if not any(corpus):
                continue
            self._indexes[field] = factory(corpus, b=LENGTH_NORM_B)

    def score_all(self, query: str) -> list[float]:
        tokens = tokenize(query)
        totals = [0.0] * len(self.entries)
        if not tokens or not self._indexes:
            return totals
        for field, index in self._indexes.items():
            weight = self.field_weights[field]
            for position, value in enumerate(index.get_scores(tokens)):
                totals[position] += weight * float(value)
        return totals

    def search(self, query: str, *, limit: int = 5) -> tuple[RankedTool, ...]:
        """上位 ``limit`` 件を返す。適応的打ち切りにより 0 件になることがある。"""
        if limit <= 0 or not self.entries:
            return ()
        scores = self.score_all(query)
        ranked = [
            RankedTool(entry=entry, score=score)
            for entry, score in zip(self.entries, scores)
            if score > 0.0
        ]
        if not ranked:
            return ()
        ranked.sort(key=lambda item: (-item.score, item.entry.id))
        cutoff = ranked[0].score * self.tau
        kept = [item for item in ranked if item.score >= cutoff]
        return tuple(kept[:limit])


def rank_tools(
    entries: Iterable[ToolEntry],
    query: str,
    *,
    field_weights: Mapping[str, float],
    limit: int = 5,
    tau: float = 0.4,
    engine: str | None = None,
) -> tuple[RankedTool, ...]:
    """1 回きりの検索に使う薄いヘルパー。"""
    return ToolRanker(tuple(entries), field_weights, tau=tau, engine=engine).search(query, limit=limit)
