"""Golden-query evaluation for `cq` search quality (FR-CQ-02).

The evaluator is intentionally independent from `mdq`: the two indexes have
different corpora and must not share an accuracy oracle that could drift.

A hit is correct only when its path equals an expected path *and* its inclusive
line range contains the expected line. Path-only agreement is never sufficient,
and hits without a usable line range are always wrong.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

PROFILES = ("hve", "app")
INTENTS = ("symbol", "substr", "regex", "trace", "natural")


class GoldenSetError(ValueError):
    """Raised when the golden query set itself is invalid (fail-closed)."""


@dataclass(frozen=True)
class Expectation:
    path: str
    line: int
    anchor: str | None = None


@dataclass(frozen=True)
class GoldenQuery:
    query: str
    profile: str
    intent: str
    expectations: tuple[Expectation, ...]


@dataclass
class EvalResult:
    total: int = 0
    top1: int = 0
    topk: int = 0
    k: int = 5
    tokens: list[int] = field(default_factory=list)
    latencies_ms: list[float] = field(default_factory=list)

    @property
    def top1_rate(self) -> float:
        return self.top1 / self.total if self.total else 0.0

    @property
    def topk_rate(self) -> float:
        return self.topk / self.total if self.total else 0.0

    @property
    def avg_tokens(self) -> float:
        return sum(self.tokens) / len(self.tokens) if self.tokens else 0.0

    @property
    def avg_latency_ms(self) -> float:
        return sum(self.latencies_ms) / len(self.latencies_ms) if self.latencies_ms else 0.0

    @property
    def max_latency_ms(self) -> float:
        return max(self.latencies_ms) if self.latencies_ms else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "k": self.k,
            "top1": self.top1,
            "topk": self.topk,
            "top1_rate": round(self.top1_rate, 4),
            "topk_rate": round(self.topk_rate, 4),
            "avg_tokens": round(self.avg_tokens, 1),
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "max_latency_ms": round(self.max_latency_ms, 1),
        }


def _hit_line_range(hit: Mapping[str, Any]) -> tuple[int, int] | None:
    lines = hit.get("lines")
    if isinstance(lines, (list, tuple)) and len(lines) == 2:
        start, end = lines
    else:
        start, end = hit.get("start_line"), hit.get("end_line")
    if isinstance(start, bool) or isinstance(end, bool):
        return None
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    return (start, end) if start <= end else (end, start)


def is_correct(hit: Mapping[str, Any], expectations: Iterable[Expectation]) -> bool:
    """True when the hit lands on an expected path *and* covers the expected line."""
    line_range = _hit_line_range(hit)
    if line_range is None:
        return False
    start, end = line_range
    path = hit.get("path")
    return any(path == e.path and start <= e.line <= end for e in expectations)


def load_golden(path: Path, repo_root: Path) -> tuple[GoldenQuery, ...]:
    """Load and validate the golden set. Any inconsistency is fail-closed."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GoldenSetError(f"cannot read golden set: {path}") from exc
    if not isinstance(raw, list) or not raw:
        raise GoldenSetError("golden set must be a non-empty list")

    queries: list[GoldenQuery] = []
    for index, item in enumerate(raw):
        where = f"entry[{index}]"
        if not isinstance(item, dict):
            raise GoldenSetError(f"{where}: must be an object")
        query, profile, intent = item.get("query"), item.get("profile"), item.get("intent")
        if not isinstance(query, str) or not query.strip():
            raise GoldenSetError(f"{where}: 'query' must be a non-empty string")
        if profile not in PROFILES:
            raise GoldenSetError(f"{where}: 'profile' must be one of {PROFILES}")
        if intent not in INTENTS:
            raise GoldenSetError(f"{where}: 'intent' must be one of {INTENTS}")
        expected = item.get("expected")
        if not isinstance(expected, list) or not expected:
            raise GoldenSetError(f"{where}: 'expected' must be a non-empty list")
        queries.append(GoldenQuery(query, profile, intent, tuple(
            _load_expectation(entry, repo_root, f"{where}.expected[{i}]")
            for i, entry in enumerate(expected)
        )))
    return tuple(queries)


def _load_expectation(raw: Any, repo_root: Path, where: str) -> Expectation:
    if not isinstance(raw, dict):
        raise GoldenSetError(f"{where}: must be an object")
    path, line, anchor = raw.get("path"), raw.get("line"), raw.get("anchor")
    if not isinstance(path, str) or not path.strip():
        raise GoldenSetError(f"{where}: 'path' must be a non-empty string")
    target = repo_root / path
    if not target.is_file():
        raise GoldenSetError(f"{where}: path does not exist: {path}")
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()

    if anchor is not None and (not isinstance(anchor, str) or not anchor):
        raise GoldenSetError(f"{where}: 'anchor' must be a non-empty string")

    if line is None:
        # anchor だけで定義する形式: 行番号のドリフトで壊れない。一意でなければ fail-closed。
        if anchor is None:
            raise GoldenSetError(f"{where}: either 'line' or 'anchor' is required")
        found = [n + 1 for n, text in enumerate(lines) if anchor in text]
        if not found:
            raise GoldenSetError(f"{where}: anchor {anchor!r} does not occur in {path}")
        if len(found) > 1:
            raise GoldenSetError(
                f"{where}: anchor {anchor!r} is ambiguous in {path} (lines {found[:5]});"
                " use a more specific anchor or pin 'line'"
            )
        return Expectation(path, found[0], anchor)

    if isinstance(line, bool) or not isinstance(line, int) or line < 1:
        raise GoldenSetError(f"{where}: 'line' must be a positive integer")
    if line > len(lines):
        raise GoldenSetError(f"{where}: line {line} exceeds {len(lines)} lines in {path}")
    if anchor is not None and anchor not in lines[line - 1]:
        found = [n + 1 for n, text in enumerate(lines) if anchor in text]
        hint = f" (found at {found[:5]})" if found else ""
        raise GoldenSetError(
            f"{where}: anchor {anchor!r} is not on line {line} of {path}{hint}"
        )
    return Expectation(path, line, anchor)


def evaluate(
    queries: Sequence[GoldenQuery],
    run: Callable[[GoldenQuery, int], tuple[Sequence[Mapping[str, Any]], int]],
    *,
    k: int = 5,
) -> EvalResult:
    """Run ``run(query, k) -> (hits, tokens)`` over the golden set and score it."""
    result = EvalResult(k=k)
    for query in queries:
        started = time.perf_counter()
        hits, tokens = run(query, k)
        result.latencies_ms.append((time.perf_counter() - started) * 1000)
        result.total += 1
        result.tokens.append(int(tokens))
        ranked = list(hits)[:k]
        if ranked and is_correct(ranked[0], query.expectations):
            result.top1 += 1
        if any(is_correct(hit, query.expectations) for hit in ranked):
            result.topk += 1
    return result
