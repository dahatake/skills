"""Benchmark harness for cq search quality (FR-CQ-02).

Control groups are measured in the same run as `cq` itself so that the token
and accuracy deltas are comparable. Correctness is delegated to the single
oracle in :mod:`cq.golden_eval`; this module never re-implements it.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from cq import golden_eval, tokens

REGEX_METACHARACTERS = set("^$*+?[]{}()|\\")


class BenchmarkError(ValueError):
    """Raised for invalid harness input (bad filter, unknown control group)."""


def repo_files(repo_root: Path) -> tuple[str, ...]:
    out = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=repo_root, text=True, encoding="utf-8",
    )
    return tuple(out.splitlines())


def _read(repo_root: Path, rel: str) -> str | None:
    try:
        return (repo_root / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _matcher(query: golden_eval.GoldenQuery):
    """Regex-intent queries are matched as regex so the control group is not handicapped."""
    if query.intent == "regex" or any(c in query.query for c in REGEX_METACHARACTERS):
        try:
            pattern = re.compile(query.query, re.IGNORECASE)
        except re.error:
            pass
        else:
            return lambda line: pattern.search(line) is not None
    needle = query.query.lower()
    return lambda line: needle in line.lower()


def grep_baseline(
    query: golden_eval.GoldenQuery, repo_root: Path, paths: Sequence[str], *, limit: int = 40
) -> tuple[list[dict[str, Any]], int]:
    """Line-oriented scan: the control group an agent would run today."""
    matches = _matcher(query)
    hits: list[dict[str, Any]] = []
    payload: list[str] = []
    for rel in paths:
        text = _read(repo_root, rel)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if matches(line):
                hits.append({"path": rel, "lines": [number, number], "snippet": line.strip()})
                payload.append(f"{rel}:{number}:{line.strip()}")
                if len(hits) >= limit:
                    return hits, tokens.count_tokens("\n".join(payload))
    return hits, tokens.count_tokens("\n".join(payload))


def readfile_baseline(
    query: golden_eval.GoldenQuery, repo_root: Path, paths: Sequence[str], *, limit: int = 3
) -> tuple[list[dict[str, Any]], int]:
    """Whole-file reads of the first matching files: the most expensive control."""
    matches = _matcher(query)
    hits: list[dict[str, Any]] = []
    payload: list[str] = []
    for rel in paths:
        text = _read(repo_root, rel)
        if text is None:
            continue
        lines = text.splitlines()
        if not any(matches(line) for line in lines):
            continue
        hits.append({"path": rel, "lines": [1, len(lines)]})
        payload.append(text)
        if len(hits) >= limit:
            break
    return hits, tokens.count_tokens("\n".join(payload))


BASELINES = {"grep": grep_baseline, "readfile": readfile_baseline}


def cq_runner(repo_root: Path, profile: str, *, db_path: Path | None = None,
              max_tokens: int = 800):
    """Return a ``run(query, k)`` callable that queries the cq index itself."""
    from cq import search

    def run(query: golden_eval.GoldenQuery, k: int):
        kwargs = {"regex": query.query} if query.intent == "regex" else {"query": query.query}
        try:
            hits = search.search(
                repo_root, profile, top_k=k, max_tokens=max_tokens,
                db_path=db_path, **kwargs,
            )
        except search.SearchError:
            return [], 0
        rows = [h.to_dict() for h in hits]
        # The agent receives one JSON line per hit, so the metadata that travels
        # with every hit is part of the cost (FR-CQ-02).
        payload = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
        return rows, tokens.count_tokens(payload)

    return run


def run_cq(
    queries: Sequence[golden_eval.GoldenQuery],
    repo_root: Path,
    profile: str,
    *,
    k: int = 5,
    db_path: Path | None = None,
) -> dict[str, Any]:
    tokens.count_tokens("warm-up")
    run = cq_runner(repo_root, profile, db_path=db_path)
    cold = golden_eval.evaluate(queries, run, k=k)
    warm = golden_eval.evaluate(queries, run, k=k)
    entry = cold.to_dict()
    entry["cold_avg_latency_ms"] = round(cold.avg_latency_ms, 1)
    entry["warm_avg_latency_ms"] = round(warm.avg_latency_ms, 1)
    entry["token_counter"] = tokens.counter_name()
    return entry


def run_baselines(
    queries: Sequence[golden_eval.GoldenQuery],
    repo_root: Path,
    paths: Sequence[str],
    *,
    k: int = 5,
    names: Sequence[str] = tuple(BASELINES),
) -> dict[str, dict[str, Any]]:
    """Measure each control group twice: pass 1 is cold, pass 2 is warm (page cache)."""
    tokens.count_tokens("warm-up")  # 初回の BPE ロードを cold 計測へ混入させない
    report: dict[str, dict[str, Any]] = {}
    for name in names:
        if name not in BASELINES:
            raise BenchmarkError(f"unknown control group: {name!r}")
        baseline = BASELINES[name]

        def run(query, _k, fn=baseline):
            return fn(query, repo_root, paths)

        cold = golden_eval.evaluate(queries, run, k=k)
        warm = golden_eval.evaluate(queries, run, k=k)
        entry = cold.to_dict()
        entry["cold_avg_latency_ms"] = round(cold.avg_latency_ms, 1)
        entry["warm_avg_latency_ms"] = round(warm.avg_latency_ms, 1)
        entry["token_counter"] = tokens.counter_name()
        report[name] = entry
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cq.benchmark")
    parser.add_argument("--golden", default="cq/golden-queries.json")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--profile", choices=golden_eval.PROFILES)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--baseline", default=",".join(BASELINES),
        help="comma separated control groups: " + ", ".join(BASELINES),
    )
    parser.add_argument("--paths", default="", help="regex filter over repository paths")
    parser.add_argument("--with-cq", action="store_true", help="also measure the cq index")
    parser.add_argument("--db", default=None, help="cq index path override")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    queries = golden_eval.load_golden(Path(args.golden), repo_root)
    if args.profile:
        queries = tuple(q for q in queries if q.profile == args.profile)
    paths = repo_files(repo_root)
    if args.paths:
        try:
            pattern = re.compile(args.paths)
        except re.error as exc:
            raise BenchmarkError(f"--paths is not a valid regex: {exc}") from exc
        paths = tuple(p for p in paths if pattern.search(p))

    report = run_baselines(
        queries, repo_root, paths, k=args.top_k,
        names=tuple(n.strip() for n in args.baseline.split(",") if n.strip()),
    )
    if args.with_cq:
        if not args.profile:
            raise BenchmarkError("--with-cq requires --profile")
        report["cq"] = run_cq(
            queries, repo_root, args.profile, k=args.top_k,
            db_path=Path(args.db) if args.db else None,
        )
    json.dump({"queries": len(queries), "files": len(paths), "baselines": report},
              sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (BenchmarkError, golden_eval.GoldenSetError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
