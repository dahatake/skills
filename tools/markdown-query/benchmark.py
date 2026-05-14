"""Benchmark `markdown-query` Skill against naive full-context baselines.

Goal
----
This Skill exists solely to minimize Context Window consumption. To support
removal decisions when alternative retrieval mechanisms become available,
this script measures, on the *current workspace*, three scenarios using the
SAME query set in the SAME process:

  - baseline_full : tokens needed to send every `.md` under the index roots
  - mdq_bm25      : tokens consumed by mdq BM25 search results
  - mdq_grep      : tokens consumed by mdq grep search results

Plus per-query wall-clock latency for the mdq scenarios (mean / p50 / p95).

Local-only. No network. No fabricated numbers — every figure printed is
measured from actual files / actual search results.

Tokenizer
---------
- tiktoken `cl100k_base` when installed (default).
- Falls back to `chars/4` and labels it `fallback(chars/4)` in the report.
- `--require-tiktoken` exits 2 when fallback would be used.

Usage
-----
    python tools/markdown-query/benchmark.py \
        --queries-file tools/markdown-query/queries.sample.txt \
        --top-k 5 --max-tokens 800 --repeat 3

    python tools/markdown-query/benchmark.py \
        --queries-json tools/markdown-query/queries.json \
        --scenarios mdq_bm25,mdq_grep \
        --ensure-index

    Note: `queries.json` is not bundled. Create one with shape
          `[{"q": "...", "expected_paths": ["path/to/doc.md"]}, ...]`.

Outputs
-------
- `<out-dir>/bench-<ISO8601>.json` (machine readable)
- `<out-dir>/bench-<ISO8601>.md`   (human readable summary)
- stdout: per-event JSON lines + final 1-line JSON summary
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import platform
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# benchmark.py lives at <repo>/tools/markdown-query/benchmark.py
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mdq import indexer, search as searcher, store  # noqa: E402

try:
    from mdq.cli import DEFAULT_ROOTS  # type: ignore[attr-defined]  # noqa: E402
except ImportError:
    # Per SKILL.md, the default index root is the current directory.
    DEFAULT_ROOTS = ["."]

# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------
try:
    import tiktoken  # type: ignore

    _ENC = tiktoken.get_encoding("cl100k_base")
    TOKENIZER = "tiktoken/cl100k_base"

    def count_tokens(text: str) -> int:
        return len(_ENC.encode(text))

except Exception:
    _ENC = None
    TOKENIZER = "fallback(chars/4)"

    def count_tokens(text: str) -> int:
        return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------
# Default-excluded path components, mirroring SKILL.md defaults plus this tool's
# own output directory. Applied to every path part to skip subtrees efficiently.
_DEFAULT_EXCLUDED_PARTS = frozenset({
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    ".mdq", "dist", "build", ".next", ".cache", "results",
})

# Fixed prompt template used for the "without skill vs with skill" comparison.
# Both scenarios use the EXACT same template so the only variable is `{context}`
# (full corpus vs mdq hits).
PROMPT_TEMPLATE = """あなたは技術文書を読み解くアシスタントです。
以下の <資料> を参考に <質問> に答えてください。

<資料>
{context}
</資料>

<質問>
{query}
</質問>
"""


def collect_baseline_text(repo_root: Path, roots: list[str],
                          path_globs: list[str] | None) -> tuple[str, dict[str, Any]]:
    """Read every .md under roots (filtered like baseline_full) and return
    (concatenated_text, stats_dict).

    The concatenated text is used as the `{context}` for the "without skill"
    prompt. Same exclusion rules as :func:`baseline_full`.
    """
    files = 0
    chars = 0
    file_list: list[str] = []
    parts: list[str] = []
    for r in roots:
        base = (repo_root / r).resolve()
        if not base.exists():
            continue
        for p in sorted(
            list(base.rglob("*.md")) + list(base.rglob("*.markdown"))
        ):
            rel_parts = p.relative_to(repo_root).parts
            if any(part in _DEFAULT_EXCLUDED_PARTS for part in rel_parts):
                continue
            rel = p.relative_to(repo_root).as_posix()
            if path_globs and not any(fnmatch.fnmatch(rel, g) for g in path_globs):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            files += 1
            chars += len(text)
            file_list.append(rel)
            parts.append(f"# FILE: {rel}\n{text}")
    concatenated = "\n\n".join(parts)
    stats = {
        "files": files,
        "chars": chars,
        "tokens": count_tokens(concatenated) if concatenated else 0,
        "roots": roots,
        "path_globs": path_globs,
        "file_list_sample": file_list[:5],
    }
    return concatenated, stats


def baseline_full(repo_root: Path, roots: list[str],
                  path_globs: list[str] | None) -> dict[str, Any]:
    """Tokens if every `.md` under roots (filtered by globs) is sent as-is.

    Skips paths whose any component is in `_DEFAULT_EXCLUDED_PARTS` so that
    measurements are not inflated by `.venv`, `node_modules`, prior benchmark
    `results/`, etc.
    """
    files = 0
    chars = 0
    tokens = 0
    file_list: list[str] = []
    for r in roots:
        base = (repo_root / r).resolve()
        if not base.exists():
            continue
        for p in sorted(
            list(base.rglob("*.md")) + list(base.rglob("*.markdown"))
        ):
            rel_parts = p.relative_to(repo_root).parts
            if any(part in _DEFAULT_EXCLUDED_PARTS for part in rel_parts):
                continue
            rel = p.relative_to(repo_root).as_posix()
            if path_globs and not any(fnmatch.fnmatch(rel, g) for g in path_globs):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            files += 1
            chars += len(text)
            tokens += count_tokens(text)
            file_list.append(rel)
    return {
        "files": files,
        "chars": chars,
        "tokens": tokens,
        "roots": roots,
        "path_globs": path_globs,
        "file_list_sample": file_list[:5],
    }


# ---------------------------------------------------------------------------
# Query loading
# ---------------------------------------------------------------------------
def load_queries(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Returns [{q: str, expected_paths: list[str] | None}, ...]."""
    out: list[dict[str, Any]] = []
    if args.queries_json:
        data = json.loads(Path(args.queries_json).read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise SystemExit("queries.json must be a JSON array")
        for item in data:
            if isinstance(item, str):
                out.append({"q": item, "expected_paths": None})
            elif isinstance(item, dict) and "q" in item:
                out.append({
                    "q": str(item["q"]),
                    "expected_paths": item.get("expected_paths") or None,
                })
            else:
                raise SystemExit(f"Invalid queries.json item: {item!r}")
    if args.queries_file:
        for line in Path(args.queries_file).read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            out.append({"q": s, "expected_paths": None})
    for q in args.q or []:
        out.append({"q": q, "expected_paths": None})
    return out


# ---------------------------------------------------------------------------
# Latency stats
# ---------------------------------------------------------------------------
def _stats_ms(samples: list[float]) -> dict[str, Any]:
    if not samples:
        return {"n": 0, "mean": None, "p50": None, "p95": None,
                "min": None, "max": None}
    n = len(samples)
    out: dict[str, Any] = {
        "n": n,
        "mean": round(statistics.fmean(samples), 3),
        "p50": round(statistics.median(samples), 3),
        "min": round(min(samples), 3),
        "max": round(max(samples), 3),
    }
    if n >= 5:
        try:
            qs = statistics.quantiles(samples, n=20, method="inclusive")
            out["p95"] = round(qs[18], 3)
        except statistics.StatisticsError:
            out["p95"] = None
    else:
        out["p95"] = None
    return out


# ---------------------------------------------------------------------------
# Search scenario runner
# ---------------------------------------------------------------------------
def run_search_scenario(conn, mode: str, queries: list[dict[str, Any]], *,
                        top_k: int, max_tokens: int, repeat: int,
                        path_globs: list[str] | None,
                        baseline_tokens_total: int,
                        baseline_context_text: str | None = None) -> dict[str, Any]:
    """Run one search scenario (bm25 or grep) for all queries.

    When ``baseline_context_text`` is provided, per-query without/with-skill
    prompt token counts are also computed using :data:`PROMPT_TEMPLATE`:
      - without_skill_prompt_tokens = tokens(template.format(context=<full corpus>, query=q))
      - with_skill_prompt_tokens    = tokens(template.format(context=<hits payload>, query=q))
    """
    per_query: list[dict[str, Any]] = []
    all_latencies_ms: list[float] = []
    total_response_tokens = 0
    total_hits = 0
    total_without_tokens = 0
    total_with_tokens = 0
    with_prompt_enabled = baseline_context_text is not None

    for entry in queries:
        q = entry["q"]
        expected = entry.get("expected_paths") or []

        # Warmup
        searcher.search(conn, q, mode=mode, top_k=top_k,
                        max_tokens=max_tokens, path_globs=path_globs)

        latencies: list[float] = []
        last_hits = []
        for _ in range(repeat):
            t0 = time.perf_counter()
            last_hits = searcher.search(
                conn, q, mode=mode, top_k=top_k,
                max_tokens=max_tokens, path_globs=path_globs,
            )
            t1 = time.perf_counter()
            latencies.append((t1 - t0) * 1000.0)

        payload = "\n".join(
            json.dumps(h.to_dict(), ensure_ascii=False) for h in last_hits
        )
        resp_tokens = count_tokens(payload)
        total_response_tokens += resp_tokens
        total_hits += len(last_hits)
        all_latencies_ms.extend(latencies)

        hit_paths = [h.path for h in last_hits]
        coverage = None
        if expected:
            matched = sum(1 for ep in expected if ep in hit_paths)
            coverage = round(matched / len(expected), 4)

        savings_pct: float | None = None
        if baseline_tokens_total > 0:
            savings_pct = round(
                (1 - resp_tokens / baseline_tokens_total) * 100, 4
            )

        # Without-skill vs With-skill prompt token comparison (uses fixed template).
        without_prompt_tokens: int | None = None
        with_prompt_tokens: int | None = None
        reduction_tokens: int | None = None
        reduction_pct: float | None = None
        if with_prompt_enabled:
            without_prompt = PROMPT_TEMPLATE.format(
                context=baseline_context_text, query=q,
            )
            with_prompt = PROMPT_TEMPLATE.format(
                context=payload, query=q,
            )
            without_prompt_tokens = count_tokens(without_prompt)
            with_prompt_tokens = count_tokens(with_prompt)
            reduction_tokens = without_prompt_tokens - with_prompt_tokens
            if without_prompt_tokens > 0:
                reduction_pct = round(
                    (reduction_tokens / without_prompt_tokens) * 100, 4
                )
            total_without_tokens += without_prompt_tokens
            total_with_tokens += with_prompt_tokens

        per_query.append({
            "query": q,
            "hits": len(last_hits),
            "hit_paths": hit_paths,
            "response_chars": len(payload),
            "response_tokens": resp_tokens,
            "vs_baseline_savings_pct": savings_pct,
            "without_skill_prompt_tokens": without_prompt_tokens,
            "with_skill_prompt_tokens": with_prompt_tokens,
            "reduction_tokens": reduction_tokens,
            "reduction_pct": reduction_pct,
            "latency_ms": _stats_ms(latencies),
            "coverage_proxy": coverage,
            "expected_paths": expected or None,
        })

    avg_tokens: float | None = None
    avg_savings: float | None = None
    avg_without: float | None = None
    avg_with: float | None = None
    avg_reduction_pct: float | None = None
    if queries:
        avg_tokens = round(total_response_tokens / len(queries), 2)
        if baseline_tokens_total > 0:
            avg_savings = round(
                (1 - avg_tokens / baseline_tokens_total) * 100, 4
            )
        if with_prompt_enabled:
            avg_without = round(total_without_tokens / len(queries), 2)
            avg_with = round(total_with_tokens / len(queries), 2)
            if avg_without > 0:
                avg_reduction_pct = round(
                    (1 - avg_with / avg_without) * 100, 4
                )

    return {
        "mode": mode,
        "queries": len(queries),
        "total_hits": total_hits,
        "avg_response_tokens": avg_tokens,
        "avg_vs_baseline_savings_pct": avg_savings,
        "avg_without_skill_prompt_tokens": avg_without,
        "avg_with_skill_prompt_tokens": avg_with,
        "avg_reduction_pct": avg_reduction_pct,
        "latency_ms_all": _stats_ms(all_latencies_ms),
        "per_query": per_query,
    }


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
def _git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT),
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        return None
    return None


def build_report(args: argparse.Namespace, queries: list[dict[str, Any]],
                 baseline: dict[str, Any] | None,
                 scenario_results: dict[str, dict[str, Any]],
                 index_summary: dict[str, Any] | None,
                 started_at: str, finished_at: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "started_at": started_at,
        "finished_at": finished_at,
        "env": {
            "tokenizer": TOKENIZER,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "commit": _git_commit(),
        },
        "params": {
            "top_k": args.top_k,
            "max_tokens": args.max_tokens,
            "repeat": args.repeat,
            "scenarios": args.scenarios,
            "roots": args.root or DEFAULT_ROOTS,
            "path_globs": args.paths,
            "queries_file": args.queries_file,
            "queries_json": args.queries_json,
            "queries_count": len(queries),
        },
        "index": index_summary,
        "baseline_full": baseline,
        "prompt_template": PROMPT_TEMPLATE,
        "scenarios": scenario_results,
    }


def write_markdown(report: dict[str, Any], path: Path) -> None:
    lines: list[str] = []
    lines.append(f"# mdq benchmark — {report['started_at']}")
    lines.append("")
    lines.append("## Environment")
    env = report["env"]
    lines.append(f"- tokenizer: `{env['tokenizer']}`")
    lines.append(f"- python: `{env['python']}`")
    lines.append(f"- platform: `{env['platform']}`")
    lines.append(f"- commit: `{env['commit']}`")
    lines.append("")
    lines.append("## Parameters")
    for k, v in report["params"].items():
        lines.append(f"- {k}: `{v}`")
    lines.append("")
    if report.get("index"):
        lines.append("## Index summary")
        lines.append("```json")
        lines.append(json.dumps(report["index"], ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")
    base = report.get("baseline_full")
    if base:
        lines.append("## baseline_full")
        lines.append(f"- files: **{base['files']}**")
        lines.append(f"- chars: **{base['chars']:,}**")
        lines.append(f"- tokens: **{base['tokens']:,}**")
        lines.append("")
    if report.get("prompt_template"):
        lines.append("## Prompt template")
        lines.append("")
        lines.append("両シナリオで同一のテンプレートを使用し、`{context}` の中身だけを")
        lines.append("「全 `.md` 連結」にした場合（Skill なし）と「mdq のヒット」にした場合「")
        lines.append("（Skill あり）でトークン数を比較します。")
        lines.append("")
        lines.append("```text")
        lines.append(report["prompt_template"].rstrip("\n"))
        lines.append("```")
        lines.append("")
    if report["scenarios"]:
        # Direct comparison: Without-skill vs With-skill (per query).
        any_with_prompt = any(
            s.get("avg_without_skill_prompt_tokens") is not None
            for s in report["scenarios"].values()
        )
        if any_with_prompt:
            lines.append("## Skill なし vs Skill あり (プロンプトトークン比較)")
            lines.append("")
            lines.append("同一のプロンプトテンプレートに対し、`{context}` に全文を詰めた場合と mdq ヒットを詰めた場合の実測トークン数。")
            lines.append("")
            for name, s in report["scenarios"].items():
                if s.get("avg_without_skill_prompt_tokens") is None:
                    continue
                lines.append(f"### {name}")
                lines.append("")
                lines.append(
                    f"- avg without skill (full corpus prompt): **{s['avg_without_skill_prompt_tokens']:,} tokens**"
                )
                lines.append(
                    f"- avg with skill (mdq hits prompt):       **{s['avg_with_skill_prompt_tokens']:,} tokens**"
                )
                avg_red = s.get("avg_reduction_pct")
                if avg_red is not None:
                    lines.append(f"- avg reduction: **{avg_red:.4f}%**")
                lines.append("")
                lines.append("| query | without skill (tokens) | with skill (tokens) | reduction (tokens) | reduction % | mean ms |")
                lines.append("|---|---:|---:|---:|---:|---:|")
                for q in s["per_query"]:
                    wo = q.get("without_skill_prompt_tokens")
                    wi = q.get("with_skill_prompt_tokens")
                    rd = q.get("reduction_tokens")
                    rp = q.get("reduction_pct")
                    wo_s = f"{wo:,}" if wo is not None else "n/a"
                    wi_s = f"{wi:,}" if wi is not None else "n/a"
                    rd_s = f"{rd:,}" if rd is not None else "n/a"
                    rp_s = f"{rp:.4f}" if rp is not None else "n/a"
                    lines.append(
                        f"| `{q['query']}` | {wo_s} | {wi_s} | {rd_s} | "
                        f"{rp_s} | {q['latency_ms']['mean']} |"
                    )
                lines.append("")

        lines.append("## Scenario summary")
        lines.append("")
        lines.append("| scenario | queries | avg tokens | avg savings vs baseline | latency mean / p50 / p95 (ms) |")
        lines.append("|---|---:|---:|---:|---|")
        for name, s in report["scenarios"].items():
            lat = s["latency_ms_all"]
            savings = (f"{s['avg_vs_baseline_savings_pct']:.4f}%"
                       if s["avg_vs_baseline_savings_pct"] is not None else "n/a")
            lines.append(
                f"| {name} | {s['queries']} | {s['avg_response_tokens']} | "
                f"{savings} | "
                f"{lat['mean']} / {lat['p50']} / {lat['p95']} |"
            )
        lines.append("")
        for name, s in report["scenarios"].items():
            lines.append(f"### {name} — per query")
            lines.append("")
            lines.append("| query | hits | tokens | savings % | mean ms | p95 ms | coverage |")
            lines.append("|---|---:|---:|---:|---:|---:|---:|")
            for q in s["per_query"]:
                cov = (f"{q['coverage_proxy']:.4f}"
                       if q["coverage_proxy"] is not None else "n/a")
                sv = (f"{q['vs_baseline_savings_pct']:.4f}"
                      if q["vs_baseline_savings_pct"] is not None else "n/a")
                lines.append(
                    f"| `{q['query']}` | {q['hits']} | {q['response_tokens']} "
                    f"| {sv} | {q['latency_ms']['mean']} "
                    f"| {q['latency_ms']['p95']} | {cov} |"
                )
            lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
SCENARIOS_ALL = ("baseline_full", "mdq_bm25", "mdq_grep")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--q", action="append", help="Query (repeatable)")
    ap.add_argument("--queries-file",
                    help="File with one query per line (# comments allowed)")
    ap.add_argument("--queries-json",
                    help='JSON array of {"q": "...", "expected_paths": [...]}')
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--max-tokens", type=int, default=800)
    ap.add_argument("--repeat", type=int, default=3,
                    help="Measured search repeats per query (warmup excluded)")
    ap.add_argument("--paths", nargs="*",
                    help="Path glob filters applied to BOTH baseline and mdq")
    ap.add_argument("--root", action="append",
                    help="Index roots (default: current directory)")
    ap.add_argument("--scenarios", default=",".join(SCENARIOS_ALL),
                    help="Comma-separated subset of: " + ",".join(SCENARIOS_ALL))
    ap.add_argument("--ensure-index", action="store_true",
                    help="Run incremental indexing before measuring")
    ap.add_argument("--db", default=str(store.DEFAULT_DB_PATH))
    ap.add_argument("--out-dir",
                    default=str(Path(__file__).parent / "results"))
    ap.add_argument("--no-markdown-report", action="store_true")
    ap.add_argument("--require-tiktoken", action="store_true",
                    help="Exit 2 if tiktoken is not available")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.require_tiktoken and _ENC is None:
        print("ERROR: tiktoken not available and --require-tiktoken was set",
              file=sys.stderr)
        return 2

    requested = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    unknown = [s for s in requested if s not in SCENARIOS_ALL]
    if unknown:
        print(f"ERROR: unknown scenarios: {unknown}", file=sys.stderr)
        return 2

    queries = load_queries(args)
    if not queries and any(s.startswith("mdq_") for s in requested):
        print("ERROR: provide --q / --queries-file / --queries-json "
              "for mdq_* scenarios", file=sys.stderr)
        return 2

    if args.repeat < 1:
        print("ERROR: --repeat must be >= 1", file=sys.stderr)
        return 2

    roots = args.root or DEFAULT_ROOTS
    conn = store.open_store(args.db)

    index_summary: dict[str, Any] | None = None
    if args.ensure_index:
        t0 = time.perf_counter()
        summary = indexer.build_index(REPO_ROOT, roots, conn)
        t1 = time.perf_counter()
        summary["index_ms"] = round((t1 - t0) * 1000.0, 3)
        index_summary = summary
        print(json.dumps({"event": "index", "summary": summary},
                         ensure_ascii=False))
    else:
        # Best-effort sanity check: any chunks indexed?
        try:
            from mdq import store as _s
            rows = _s.all_chunks(conn)
            if not rows and any(s.startswith("mdq_") for s in requested):
                print("ERROR: no chunks in index. Pass --ensure-index or run "
                      "`mdq index` (or `python -m mdq index`) first.",
                      file=sys.stderr)
                return 2
        except Exception as exc:
            print(f"ERROR: index check failed: {exc}", file=sys.stderr)
            return 2

    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    baseline: dict[str, Any] | None = None
    baseline_context_text: str | None = None
    if "baseline_full" in requested:
        baseline_context_text, baseline = collect_baseline_text(
            REPO_ROOT, roots, args.paths,
        )
        print(json.dumps({"event": "baseline_full", "data": baseline},
                         ensure_ascii=False))
    elif any(s.startswith("mdq_") for s in requested):
        # baseline_full を scenarios から外しても prompt 比較は出したいので
        # コーパスを読み込んでおく（「baseline_full」セクションは出さない）。
        baseline_context_text, _ = collect_baseline_text(
            REPO_ROOT, roots, args.paths,
        )

    baseline_tokens_total = baseline["tokens"] if baseline else 0

    scenario_results: dict[str, dict[str, Any]] = {}
    for scenario in requested:
        if scenario == "baseline_full":
            continue
        mode = "bm25" if scenario == "mdq_bm25" else "grep"
        r = run_search_scenario(
            conn, mode, queries,
            top_k=args.top_k, max_tokens=args.max_tokens,
            repeat=args.repeat, path_globs=args.paths,
            baseline_tokens_total=baseline_tokens_total,
            baseline_context_text=baseline_context_text,
        )
        scenario_results[scenario] = r
        print(json.dumps({
            "event": "scenario",
            "name": scenario,
            "avg_response_tokens": r["avg_response_tokens"],
            "avg_savings_pct": r["avg_vs_baseline_savings_pct"],
            "avg_without_skill_prompt_tokens": r.get("avg_without_skill_prompt_tokens"),
            "avg_with_skill_prompt_tokens": r.get("avg_with_skill_prompt_tokens"),
            "avg_reduction_pct": r.get("avg_reduction_pct"),
            "latency_ms_all": r["latency_ms_all"],
        }, ensure_ascii=False))

    finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = build_report(args, queries, baseline, scenario_results,
                          index_summary, started_at, finished_at)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = started_at.replace(":", "").replace("-", "")
    json_path = out_dir / f"bench-{stamp}.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                         encoding="utf-8")

    md_path: Path | None = None
    if not args.no_markdown_report:
        md_path = out_dir / f"bench-{stamp}.md"
        write_markdown(report, md_path)

    summary_line = {
        "event": "summary",
        "json_report": str(json_path.relative_to(REPO_ROOT).as_posix()),
        "markdown_report": (str(md_path.relative_to(REPO_ROOT).as_posix())
                            if md_path else None),
        "tokenizer": TOKENIZER,
        "queries": len(queries),
        "scenarios": list(scenario_results.keys()),
    }
    print(json.dumps(summary_line, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
