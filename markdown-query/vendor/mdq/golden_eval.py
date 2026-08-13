"""Golden-query scoring for markdown-query (FR-MDQ-01 / FR-MDQ-04).

A hit is correct only when its repository-relative path matches and its inclusive
line range contains the expected line.  This module is the single correctness
oracle used by the benchmark; benchmark code must not duplicate the judgement.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Optional, Sequence

SCHEMA_VERSION = 1


class GoldenSetError(ValueError):
    """The golden set is unreadable, malformed, or stale."""


@dataclass(frozen=True)
class ExpectedHit:
    path: str
    line: int
    anchor: str = ""


@dataclass(frozen=True)
class GoldenQuery:
    id: str
    q: str
    expected: tuple[ExpectedHit, ...]
    paths: tuple[str, ...] = ()
    group: str = ""
    note: str = ""


def _norm_path(path: str) -> str:
    normalised = str(path).replace("\\", "/")
    while normalised.startswith("./"):
        normalised = normalised[2:]
    return normalised


def _validate_relative_path(path: str, where: str) -> str:
    normalised = _norm_path(path)
    posix = PurePosixPath(normalised)
    first = posix.parts[0] if posix.parts else ""
    if (
        not normalised
        or posix.is_absolute()
        or ".." in posix.parts
        or ":" in first
        or normalised != posix.as_posix()
    ):
        raise GoldenSetError(f"{where}: path must be repository-relative: {path!r}")
    return normalised


def _hit_fields(hit: Any) -> tuple[Optional[str], Optional[int], Optional[int]]:
    if isinstance(hit, dict):
        path = hit.get("path")
        start = hit.get("start_line")
        end = hit.get("end_line")
        if start is None or end is None:
            lines = hit.get("lines")
            if isinstance(lines, (list, tuple)) and len(lines) == 2:
                start, end = lines
    else:
        path = getattr(hit, "path", None)
        start = getattr(hit, "start_line", None)
        end = getattr(hit, "end_line", None)
    if path is None or isinstance(start, bool) or isinstance(end, bool):
        return (None if path is None else str(path)), None, None
    if not isinstance(start, int) or not isinstance(end, int):
        return str(path), None, None
    if start > end:
        return str(path), None, None
    return str(path), start, end


def hit_matches(hit: Any, expected: ExpectedHit) -> bool:
    """Return true for path equality plus inclusive line-range containment."""
    path, start, end = _hit_fields(hit)
    if path is None or start is None or end is None:
        return False
    return _norm_path(path) == _norm_path(expected.path) and start <= expected.line <= end


def rank_of_first_correct(
    hits: Sequence[Any], expected: Sequence[ExpectedHit]
) -> Optional[int]:
    for rank, hit in enumerate(hits, start=1):
        if any(hit_matches(hit, item) for item in expected):
            return rank
    return None


def score_query(
    hits: Sequence[Any], expected: Sequence[ExpectedHit], top_k: int
) -> dict[str, Any]:
    rank = rank_of_first_correct(hits, expected)
    return {
        "rank": rank,
        "top1": rank == 1,
        "topk": rank is not None and rank <= top_k,
    }


def _mean_reciprocal_rank(
    rows: Sequence[dict[str, Any]], top_k: int
) -> Optional[float]:
    """Mean of 1/rank over ``rows``; a miss inside top-k contributes 0.

    Returns ``None`` when any row omits ``rank``: scoring those as misses would
    report an MRR that contradicts the row's own ``topk`` flag.
    """
    if not all("rank" in row for row in rows):
        return None
    hit_total = sum(
        1.0 / row["rank"]
        for row in rows
        if isinstance(row["rank"], int) and 1 <= row["rank"] <= top_k
    )
    return round(hit_total / len(rows), 4)


def aggregate(per_query: Iterable[dict[str, Any]], top_k: int) -> dict[str, Any]:
    rows = list(per_query)
    if not rows:
        return {
            "queries": 0,
            "k": top_k,
            "top1_accuracy": None,
            "topk_accuracy": None,
            "mrr_at_k": None,
        }
    total = len(rows)
    return {
        "queries": total,
        "k": top_k,
        "top1_accuracy": round(sum(bool(row.get("top1")) for row in rows) / total, 4),
        "topk_accuracy": round(sum(bool(row.get("topk")) for row in rows) / total, 4),
        "mrr_at_k": _mean_reciprocal_rank(rows, top_k),
    }


def _read_lines(repo_root: Path, rel_path: str, query_id: str) -> list[str]:
    root = repo_root.resolve()
    target = (root / rel_path).resolve()
    if not target.is_relative_to(root):
        raise GoldenSetError(f"{query_id}: expected path escapes repository: {rel_path}")
    if not target.is_file():
        raise GoldenSetError(f"{query_id}: expected path does not exist: {rel_path}")
    try:
        return target.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError as exc:
        raise GoldenSetError(f"{query_id}: cannot read expected path: {rel_path}") from exc


def _load_expected(raw: Any, repo_root: Path, query_id: str, index: int) -> ExpectedHit:
    where = f"{query_id}.expected[{index}]"
    if not isinstance(raw, dict):
        raise GoldenSetError(f"{where}: expected must be an object")
    path = raw.get("path")
    if not isinstance(path, str) or not path.strip():
        raise GoldenSetError(f"{where}: path must be a non-empty string")
    path = _validate_relative_path(path, where)
    anchor = raw.get("anchor", "")
    if anchor is None:
        anchor = ""
    if not isinstance(anchor, str):
        raise GoldenSetError(f"{where}: anchor must be a string")
    if path.startswith("hve-dev/") and path.endswith(".csv") and not anchor:
        raise GoldenSetError(f"{where}: generated inventory expectation requires anchor")

    lines = _read_lines(repo_root, path, query_id)
    line = raw.get("line")
    if line is not None and (isinstance(line, bool) or not isinstance(line, int) or line < 1):
        raise GoldenSetError(f"{where}: line must be a positive integer")
    if line is not None and line > len(lines):
        raise GoldenSetError(
            f"{where}: line {line} exceeds {len(lines)} lines in {path}"
        )

    found: list[int] = []
    if anchor:
        found = [number for number, text in enumerate(lines, 1) if anchor in text]
        if not found:
            raise GoldenSetError(f"{where}: anchor {anchor!r} does not occur in {path}")
        if len(found) > 1:
            raise GoldenSetError(
                f"{where}: anchor {anchor!r} is ambiguous in {path}; lines={found[:10]}"
            )

    if line is None:
        if not anchor:
            raise GoldenSetError(f"{where}: either line or anchor is required")
        line = found[0]
    elif anchor and found[0] != line:
        raise GoldenSetError(
            f"{where}: anchor {anchor!r} is not on line {line} of {path}; found at {found}"
        )
    return ExpectedHit(path=path, line=line, anchor=anchor)


def load_golden(path: Path, repo_root: Path) -> tuple[GoldenQuery, ...]:
    """Load and fully validate a golden set against ``repo_root``."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise GoldenSetError(f"cannot read golden set: {path}") from exc
    except json.JSONDecodeError as exc:
        raise GoldenSetError(f"golden set is invalid JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise GoldenSetError("golden set root must be an object")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise GoldenSetError(
            f"unsupported schema_version={raw.get('schema_version')!r}; expected {SCHEMA_VERSION}"
        )
    entries = raw.get("queries")
    if not isinstance(entries, list) or not entries:
        raise GoldenSetError("queries must be a non-empty array")

    queries: list[GoldenQuery] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(entries):
        where = f"queries[{index}]"
        if not isinstance(item, dict):
            raise GoldenSetError(f"{where}: query must be an object")
        query_id = item.get("id")
        query = item.get("q")
        if not isinstance(query_id, str) or not query_id.strip():
            raise GoldenSetError(f"{where}: id must be a non-empty string")
        if query_id in seen_ids:
            raise GoldenSetError(f"duplicate id: {query_id}")
        seen_ids.add(query_id)
        if not isinstance(query, str) or not query.strip():
            raise GoldenSetError(f"{where}: q must be a non-empty string")
        expected_raw = item.get("expected")
        if not isinstance(expected_raw, list) or not expected_raw:
            raise GoldenSetError(f"{query_id}: expected must be a non-empty array")
        paths_raw = item.get("paths", [])
        if not isinstance(paths_raw, list) or not all(
            isinstance(value, str) and value for value in paths_raw
        ):
            raise GoldenSetError(f"{query_id}: paths must be an array of strings")
        expected = tuple(
            _load_expected(value, repo_root, query_id, expected_index)
            for expected_index, value in enumerate(expected_raw)
        )
        queries.append(
            GoldenQuery(
                id=query_id,
                q=query,
                expected=expected,
                paths=tuple(paths_raw),
                group=str(item.get("group") or ""),
                note=str(item.get("note") or ""),
            )
        )
    return tuple(queries)


def validate_golden(queries: Sequence[GoldenQuery], repo_root: Path) -> list[str]:
    """Re-check already loaded expectations; return errors for compatibility."""
    errors: list[str] = []
    for query in queries:
        for expected in query.expected:
            try:
                lines = _read_lines(repo_root, expected.path, query.id)
            except GoldenSetError as exc:
                errors.append(str(exc))
                continue
            if expected.line < 1 or expected.line > len(lines):
                errors.append(
                    f"{query.id}: line {expected.line} outside {expected.path} ({len(lines)} lines)"
                )
            elif expected.anchor and expected.anchor not in lines[expected.line - 1]:
                errors.append(
                    f"{query.id}: anchor missing at {expected.path}:{expected.line}"
                )
    return errors
