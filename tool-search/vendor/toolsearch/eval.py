"""HVE Tool Search — 評価ハーネス（FR-TS-05）。

2 つを測る:

1. **検索品質** — golden クエリ集合に対する Recall@k / MRR
2. **コスト** — 全ツール定義を前置きした場合と、pin のみ + 検索返却分の推定トークン量の比

いずれも決定的（乱数・ネットワークを使わない）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .ranking import ToolRanker
from .types import ToolEntry

GOLDEN_FILE = Path(__file__).with_name("golden-tool-queries.json")

# 1 回の検索で返す想定件数（policy.limit と同じ既定）。トークン削減率の分子に使う。
DEFAULT_HITS_PER_TURN = 5


@dataclass(frozen=True)
class GoldenQuery:
    query: str
    expected: tuple[str, ...]
    note: str = ""


@dataclass(frozen=True)
class QueryResult:
    query: str
    expected: tuple[str, ...]
    ranked: tuple[str, ...]

    def hit_rank(self) -> int | None:
        """最初に正解が現れた 1 始まりの順位。無ければ None。"""
        for position, name in enumerate(self.ranked, start=1):
            if name in self.expected:
                return position
        return None

    def recall_at(self, k: int) -> float:
        if not self.expected:
            return 0.0
        found = sum(1 for name in self.ranked[:k] if name in self.expected)
        return found / len(self.expected)


@dataclass(frozen=True)
class EvalReport:
    results: tuple[QueryResult, ...]

    @property
    def total(self) -> int:
        return len(self.results)

    def recall_at(self, k: int) -> float:
        if not self.results:
            return 0.0
        return sum(r.recall_at(k) for r in self.results) / len(self.results)

    @property
    def mrr(self) -> float:
        if not self.results:
            return 0.0
        total = 0.0
        for result in self.results:
            rank = result.hit_rank()
            if rank:
                total += 1.0 / rank
        return total / len(self.results)

    @property
    def misses(self) -> tuple[QueryResult, ...]:
        return tuple(r for r in self.results if r.hit_rank() is None)


@dataclass(frozen=True)
class TokenReport:
    baseline_tokens: int
    optimized_tokens: int

    @property
    def reduction_ratio(self) -> float:
        if self.baseline_tokens <= 0:
            return 0.0
        return 1.0 - (self.optimized_tokens / self.baseline_tokens)


def load_golden(path: Path | str | None = None) -> tuple[GoldenQuery, ...]:
    target = Path(path) if path is not None else GOLDEN_FILE
    raw = json.loads(target.read_text(encoding="utf-8"))
    return tuple(
        GoldenQuery(
            query=item["query"],
            expected=tuple(item["expected"]),
            note=item.get("note", ""),
        )
        for item in raw["queries"]
    )


def evaluate(
    entries: Sequence[ToolEntry],
    golden: Iterable[GoldenQuery],
    *,
    field_weights: Mapping[str, float],
    tau: float = 0.0,
    limit: int = 10,
) -> EvalReport:
    """Recall 測定では適応的打ち切りを外す（既定 ``tau=0.0``）。

    打ち切りは実運用でノイズを減らすための仕組みであり、ランキング能力そのものの
    評価には上位 ``limit`` 件を素直に見る方が実態を表す。
    """
    ranker = ToolRanker(entries, field_weights, tau=tau)
    results = [
        QueryResult(
            query=item.query,
            expected=item.expected,
            ranked=tuple(hit.entry.name for hit in ranker.search(item.query, limit=limit)),
        )
        for item in golden
    ]
    return EvalReport(results=tuple(results))


def estimate_tokens(text: str) -> int:
    """`tiktoken` があれば使い、無ければ文字数 / 4 で概算する。"""
    if not text:
        return 0
    try:
        import tiktoken  # type: ignore[import-not-found]

        return len(tiktoken.get_encoding("cl100k_base").encode(text))
    except Exception:
        return max(1, len(text) // 4)


def entry_definition_text(entry: ToolEntry) -> str:
    """モデルへ渡るツール定義の相当テキスト（検索専用語彙は含めない）。"""
    parts = [entry.name, entry.description]
    parts.extend(entry.arg_terms)
    return "\n".join(part for part in parts if part)


def token_report(
    all_entries: Sequence[ToolEntry],
    pinned: Sequence[ToolEntry],
    *,
    hits_per_turn: int = DEFAULT_HITS_PER_TURN,
) -> TokenReport:
    """全定義前置き（baseline）と pin + 検索返却分（optimized）の推定トークン量。

    pin 判定は **``ToolEntry.id`` で行う**。``apply_policy`` は pin や検索語彙を差し替えた
    新しい ``ToolEntry`` を返すため、オブジェクト同一性では元のエントリと一致しない。
    """
    baseline = sum(estimate_tokens(entry_definition_text(e)) for e in all_entries)
    pinned_tokens = sum(estimate_tokens(entry_definition_text(e)) for e in pinned)

    pinned_ids = {e.id for e in pinned}
    searchable = [e for e in all_entries if e.id not in pinned_ids]
    if searchable:
        average = sum(estimate_tokens(entry_definition_text(e)) for e in searchable) / len(searchable)
    else:
        average = 0.0
    returned = int(round(average * min(hits_per_turn, len(searchable))))

    return TokenReport(baseline_tokens=baseline, optimized_tokens=pinned_tokens + returned)


def format_report(report: EvalReport, tokens: TokenReport | None = None) -> str:
    lines = [
        f"queries      : {report.total}",
        f"recall@5     : {report.recall_at(5):.3f}",
        f"recall@10    : {report.recall_at(10):.3f}",
        f"MRR          : {report.mrr:.3f}",
        f"misses       : {len(report.misses)}",
    ]
    if tokens is not None:
        lines += [
            f"tokens base  : {tokens.baseline_tokens}",
            f"tokens opt   : {tokens.optimized_tokens}",
            f"reduction    : {tokens.reduction_ratio:.1%}",
        ]
    for miss in report.misses:
        lines.append(f"  MISS {miss.query!r} expected={list(miss.expected)}")
    return "\n".join(lines)
