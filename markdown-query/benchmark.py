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

Run this from the root of the repository being measured: the index roots, the
`mdq.toml` and the git provenance are all resolved against the current working
directory, exactly like `python -m mdq index`.

Tokenizer
---------
- tiktoken `cl100k_base` when installed (default).
- Falls back to `chars/4` and labels it `fallback(chars/4)` in the report.
- `--require-tiktoken` exits 2 when fallback would be used.

Usage
-----
    python <kit>/benchmark.py \
        --queries-file <kit>/queries.sample.txt \
        --top-k 5 --max-tokens 800 --repeat 3

    python <kit>/benchmark.py \
        --queries-json my-queries.json \
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
import hashlib
import json
import platform
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# benchmark.py sits at the root of the distribution kit, next to vendor/.
_KIT_ROOT = Path(__file__).resolve().parent
_VENDOR = _KIT_ROOT / "vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

# The repository being benchmarked. The kit may sit anywhere inside it, so the
# contract is the same as `mdq index`: run this from the repository root.
REPO_ROOT = Path.cwd()

for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            # Keep the platform's encoding; only stop an unencodable character
            # (an em dash in --help on a cp932 console) from aborting the run.
            _reconfigure(errors="replace")
        except (ValueError, OSError):
            pass

from mdq import golden_eval, indexer, search as searcher, store  # noqa: E402

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

    _ENC: Any = tiktoken.get_encoding("cl100k_base")
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
        for p in sorted(base.rglob("*.md")):
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
def _resolve_repo_input(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path


def load_queries(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Returns [{q: str, expected_paths: list[str] | None}, ...].

    ``--golden`` を渡した場合は ``expected_hits`` (list[ExpectedHit]) と
    per-query ``paths`` を追加し、FR-MDQ-01 の top-1 / top-k 採点を有効にする。
    """
    out: list[dict[str, Any]] = []
    if getattr(args, "golden", None):
        golden_path = _resolve_repo_input(args.golden)
        try:
            gqs = golden_eval.load_golden(golden_path, REPO_ROOT)
        except golden_eval.GoldenSetError as exc:
            raise SystemExit(f"ERROR: {exc}")
        for gq in gqs:
            out.append({
                "id": gq.id,
                "q": gq.q,
                "group": gq.group,
                "expected_paths": [e.path for e in gq.expected],
                "expected_hits": list(gq.expected),
                "paths": list(gq.paths) or None,
            })
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
# Golden scoring (FR-MDQ-01; judgement itself lives in mdq.golden_eval)
# ---------------------------------------------------------------------------
def _golden_summary(per_query: list[dict[str, Any]],
                    top_k: int) -> dict[str, Any] | None:
    """Aggregate golden scores overall and per group. None when not scored."""
    scored = [r for r in per_query if "top1" in r]
    if not scored:
        return None
    out: dict[str, Any] = {"overall": golden_eval.aggregate(scored, top_k)}
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in scored:
        groups.setdefault(r.get("group") or "(none)", []).append(r)
    out["per_group"] = {
        name: golden_eval.aggregate(rows, top_k)
        for name, rows in sorted(groups.items())
    }
    return out


# ---------------------------------------------------------------------------
# Search scenario runner
# ---------------------------------------------------------------------------
def run_search_scenario(conn, mode: str, queries: list[dict[str, Any]], *,
                        top_k: int, max_tokens: int, repeat: int,
                        path_globs: list[str] | None,
                        baseline_tokens_total: int,
                        ignore_golden_paths: bool = False) -> dict[str, Any]:
    """Run one search scenario (bm25 or grep) for all queries."""
    per_query: list[dict[str, Any]] = []
    all_latencies_ms: list[float] = []
    total_response_tokens = 0
    total_hits = 0

    for entry in queries:
        q = entry["q"]
        expected = entry.get("expected_paths") or []
        expected_hits = entry.get("expected_hits") or []
        entry_paths = path_globs if ignore_golden_paths else (
            entry.get("paths") or path_globs
        )

        # Warmup
        searcher.search(conn, q, mode=mode, top_k=top_k,
                        max_tokens=max_tokens, path_globs=entry_paths)

        latencies: list[float] = []
        last_hits = []
        for _ in range(repeat):
            t0 = time.perf_counter()
            last_hits = searcher.search(
                conn, q, mode=mode, top_k=top_k,
                max_tokens=max_tokens, path_globs=entry_paths,
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

        row: dict[str, Any] = {
            "query": q,
            "hits": len(last_hits),
            "hit_paths": hit_paths,
            "response_chars": len(payload),
            "response_tokens": resp_tokens,
            "vs_baseline_savings_pct": savings_pct,
            "latency_ms": _stats_ms(latencies),
            "coverage_proxy": coverage,
            "expected_paths": expected or None,
        }
        if expected_hits:
            row["id"] = entry.get("id")
            row["group"] = entry.get("group")
            row.update(golden_eval.score_query(last_hits, expected_hits, top_k))
        per_query.append(row)

    avg_tokens: float | None = None
    avg_savings: float | None = None
    if queries:
        avg_tokens = round(total_response_tokens / len(queries), 2)
        if baseline_tokens_total > 0:
            avg_savings = round(
                (1 - avg_tokens / baseline_tokens_total) * 100, 4
            )

    return {
        "mode": mode,
        "queries": len(queries),
        "total_hits": total_hits,
        "avg_response_tokens": avg_tokens,
        "avg_vs_baseline_savings_pct": avg_savings,
        "latency_ms_all": _stats_ms(all_latencies_ms),
        "golden": _golden_summary(per_query, top_k),
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


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_tree(paths: list[Path], base: Path) -> str:
    """Hash relative names and bytes so equal trees have one audit value."""
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.as_posix()):
        relative = path.relative_to(base).as_posix().encode("utf-8")
        digest.update(relative + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _dependency_sha256() -> str | None:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "freeze", "--all"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    normalized = "\n".join(sorted(line.strip() for line in result.stdout.splitlines()
                                    if line.strip())) + "\n"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _build_provenance(args: argparse.Namespace) -> dict[str, Any]:
    golden_path = (_resolve_repo_input(args.golden).resolve()
                   if getattr(args, "golden", None) else None)
    engine_paths = [
        path for path in (_VENDOR / "mdq").rglob("*.py")
        if path.is_file() and "__pycache__" not in path.parts
    ]
    return {
        "engine_sha256": _sha256_tree(engine_paths, _VENDOR),
        "config_sha256": _sha256_file(REPO_ROOT / "mdq.toml"),
        "golden_sha256": _sha256_file(golden_path) if golden_path else None,
        "dependency_sha256": _dependency_sha256(),
    }


def _report_path_for_summary(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def build_report(args: argparse.Namespace, queries: list[dict[str, Any]],
                 baseline: dict[str, Any] | None,
                 scenario_results: dict[str, dict[str, Any]],
                 index_summary: dict[str, Any] | None,
                 started_at: str, finished_at: str,
                 database_paths: dict[str, str]) -> dict[str, Any]:
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
            "golden": getattr(args, "golden", None),
            "ignore_golden_paths": bool(getattr(args, "ignore_golden_paths", False)),
            "db": args.db,
            "queries_count": len(queries),
        },
        "database_paths": dict(sorted(database_paths.items())),
        "provenance": _build_provenance(args),
        "index": index_summary,
        "baseline_full": baseline,
        "scenarios": scenario_results,
    }


def write_markdown(report: dict[str, Any], path: Path) -> None:
    lines: list[str] = []
    lines.append(f"# mdq benchmark — {report['started_at']}")
    lines.append("")
    lines.append("> **このレポートで測っていること**")
    lines.append(">")
    lines.append("> `markdown-query` (mdq) Skill を使うことで、"
                 "**LLM に渡すコンテキスト（= 消費トークン）が、Skillなしで全 Markdown を直接渡す場合と比べてどれだけ減るか**、"
                 "そして **検索レスポンスがどれだけ速いか** を、"
                 "OpenAI 系モデルで使われる `tiktoken` の `cl100k_base` トークナイザで実測したものです。")
    lines.append("")
    lines.append("- **Skillなし（baseline_full）**: 該当ディレクトリ配下の `.md` をすべて連結して LLM に渡したときのトークン数")
    lines.append("- **Skillあり（mdq_bm25 / mdq_grep）**: 同じ質問を mdq の検索（BM25 / grep）で投げ、ヒットした該当チャンクだけを LLM に渡したときのトークン数")
    lines.append("")
    lines.append("## Environment（実行環境）")
    lines.append("")
    lines.append("計測に使ったトークナイザ・実行マシン・Git commit。`tokenizer` が `fallback(chars/4)` の場合は tiktoken 未導入のため文字数÷4 で近似していることを意味します。")
    lines.append("")
    env = report["env"]
    lines.append(f"- tokenizer: `{env['tokenizer']}`")
    lines.append(f"- python: `{env['python']}`")
    lines.append(f"- platform: `{env['platform']}`")
    lines.append(f"- commit: `{env['commit']}`")
    lines.append("")
    lines.append("## Parameters（実行パラメータ）")
    lines.append("")
    lines.append("ベンチマーク呼び出し時の引数。`roots` 配下の `.md` がベースライン対象、`queries_count` 件の質問を `repeat` 回ずつ計測しています。")
    lines.append("")
    for k, v in report["params"].items():
        lines.append(f"- {k}: `{v}`")
    lines.append("")

    # Prompts used (queries themselves are the prompts sent to mdq).
    queries_params = report["params"].get("queries_count", 0)
    if queries_params:
        lines.append("## 比較に使った Prompt（検索クエリ）")
        lines.append("")
        lines.append("mdq に投げた検索文字列の一覧です。Skillなし側ではこれらの質問に答えるために "
                     "全 Markdown を LLM に渡す前提でトークン数を算出しています。")
        lines.append("")
        # Pull prompts from the first scenario's per_query (all scenarios share them).
        prompts_listed: list[str] = []
        for _name, s in report["scenarios"].items():
            for q in s["per_query"]:
                if q["query"] not in prompts_listed:
                    prompts_listed.append(q["query"])
            break
        for i, p in enumerate(prompts_listed, 1):
            lines.append(f"{i}. `{p}`")
        lines.append("")

    if report.get("index"):
        lines.append("## Index summary（mdq インデックス構築結果）")
        lines.append("")
        lines.append("mdq が SQLite に格納したファイル/チャンク数と所要時間。"
                     "`files_indexed` がベースラインのファイル数とほぼ一致していれば対象を正しく拾えています。")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(report["index"], ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")
    base = report.get("baseline_full")
    if base:
        lines.append("## Skillなしの場合のトークン消費（baseline_full）")
        lines.append("")
        lines.append("`roots` 配下の全 `.md` を **1リクエストにそのまま貼り付けて LLM に渡した場合** の合計トークン数。"
                     "この値が比較の分母（= Skillなしのコスト）になります。")
        lines.append("")
        lines.append(f"- 対象ファイル数: **{base['files']}**")
        lines.append(f"- 合計文字数: **{base['chars']:,}**")
        lines.append(f"- **合計トークン数（= Skillなしのコスト）: {base['tokens']:,} tokens**")
        lines.append("")

    base_tokens = base["tokens"] if base else 0

    if report["scenarios"] and base_tokens:
        lines.append("## Skillなし vs Skillあり 直接比較")
        lines.append("")
        lines.append("各 Prompt について、")
        lines.append("「Skillなし（全 md を貼り付け）= 上の baseline トークン数」と "
                     "「Skillあり（mdq の検索結果のみ）」を並べて、削減トークン数・削減率を出しています。")
        lines.append("")
        for name, s in report["scenarios"].items():
            mode_label = "BM25 検索（意味的にスコアリング）" if name == "mdq_bm25" else "grep 検索（部分一致）"
            lines.append(f"### Skill: `{name}` — {mode_label}")
            lines.append("")
            lines.append("| # | Prompt | Skillなし tokens | Skillあり tokens | 削減 tokens | 削減率 | レスポンス mean (ms) |")
            lines.append("|---:|---|---:|---:|---:|---:|---:|")
            for i, q in enumerate(s["per_query"], 1):
                skill_tok = q["response_tokens"]
                reduced = base_tokens - skill_tok
                sv = (f"{q['vs_baseline_savings_pct']:.2f}%"
                      if q.get("vs_baseline_savings_pct") is not None else "n/a")
                lines.append(
                    f"| {i} | `{q['query']}` | {base_tokens:,} | {skill_tok:,} | "
                    f"{reduced:,} | {sv} | {q['latency_ms']['mean']} |"
                )
            # Summary row for the scenario.
            if s["avg_response_tokens"] is not None:
                avg_tok = s["avg_response_tokens"]
                avg_reduced = base_tokens - avg_tok
                avg_sv = (f"{s['avg_vs_baseline_savings_pct']:.2f}%"
                          if s["avg_vs_baseline_savings_pct"] is not None else "n/a")
                lines.append(
                    f"| — | **平均** | **{base_tokens:,}** | **{avg_tok}** | "
                    f"**{avg_reduced:,.0f}** | **{avg_sv}** | "
                    f"**{s['latency_ms_all']['mean']}** |"
                )
            lines.append("")

    if report["scenarios"]:
        golden_scenarios = {n: s for n, s in report["scenarios"].items()
                            if s.get("golden")}
        if golden_scenarios:
            lines.append("## Golden query 正解率（FR-MDQ-01）")
            lines.append("")
            lines.append("正解判定は「ヒットの path が期待 path と一致し、かつヒットの行範囲が"
                         "期待行番号を含む」ことをもって行います（パス一致のみは不正解）。"
                         "判定実装は `mdq/golden_eval.py` の単一実装です。"
                         "MRR@k は先頭 k 件のうち最初の正解の順位の逆数の平均で、"
                         "k 件内に正解が無いクエリの寄与は 0 です。")
            lines.append("")
            lines.append("| scenario | group | queries | top-1 正解率 | top-k 正解率 | MRR@k |")
            lines.append("|---|---|---:|---:|---:|---:|")
            for name, s in golden_scenarios.items():
                g = s["golden"]
                for label, agg in [("(overall)", g["overall"])] + \
                        sorted(g.get("per_group", {}).items()):
                    t1 = ("n/a" if agg["top1_accuracy"] is None
                          else f"{agg['top1_accuracy'] * 100:.1f}%")
                    tk = ("n/a" if agg["topk_accuracy"] is None
                          else f"{agg['topk_accuracy'] * 100:.1f}%")
                    mrr = ("n/a" if agg["mrr_at_k"] is None
                           else f"{agg['mrr_at_k']:.4f}")
                    lines.append(
                        f"| {name} | {label} | {agg['queries']} | {t1} | {tk} | {mrr} |"
                    )
            lines.append("")

        lines.append("## Scenario summary（シナリオ集計）")
        lines.append("")
        lines.append("各シナリオの全クエリ平均をまとめた表。"
                     "`avg tokens` が小さいほど LLM への入力が軽く、"
                     "`avg savings vs baseline` が高いほど Skill による削減効果が大きいことを示します。"
                     "`latency` は `--repeat` 回の各クエリ実行時間 (ms) を全クエリ横断で集計したもので、"
                     "mean=平均 / p50=中央値 / p95=遅い側 5% を切ったあたり、です。")
        lines.append("")
        lines.append("| scenario | queries | avg tokens | avg savings vs baseline | latency mean / p50 / p95 (ms) |")
        lines.append("|---|---:|---:|---:|---|")
        for name, s in report["scenarios"].items():
            lat = s["latency_ms_all"]
            savings = (f"{s['avg_vs_baseline_savings_pct']:.4f}%"
                       if s["avg_vs_baseline_savings_pct"] is not None else "n/a")
            lines.append(
                f"| {name} | {s.get('queries', len(s['per_query']))} | "
                f"{s['avg_response_tokens']} | "
                f"{savings} | "
                f"{lat['mean']} / {lat['p50']} / {lat['p95']} |"
            )
        lines.append("")
        lines.append("## クエリ別の詳細")
        lines.append("")
        lines.append("各列の意味:")
        lines.append("")
        lines.append("- **hits**: mdq がヒットとして返したチャンク数（最大 `top_k`）")
        lines.append("- **tokens**: そのヒット集合を LLM に渡す場合のトークン数（= Skillあり時のコスト）")
        lines.append("- **savings %**: baseline_full の全文トークン数に対する削減率（= 1 − tokens / baseline_tokens）")
        lines.append("- **mean ms / p95 ms**: 検索処理だけの所要時間。`--repeat` 回の計測値から算出（p95 は n≥5 のときのみ）")
        lines.append("- **coverage**: `queries.json` で `expected_paths` を指定したときのみ算出される、期待パスがヒットに含まれた割合")
        lines.append("- **rank**: `--golden` 指定時のみ。期待行を含むチャンクが何位に現れたか（`-` = top-k 内になし）")
        lines.append("")
        for name, s in report["scenarios"].items():
            lines.append(f"### {name} — per query")
            lines.append("")
            lines.append("| query | hits | tokens | savings % | mean ms | p95 ms | coverage | rank |")
            lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
            for q in s["per_query"]:
                cov = (f"{q['coverage_proxy']:.4f}"
                       if q.get("coverage_proxy") is not None else "n/a")
                sv = (f"{q['vs_baseline_savings_pct']:.4f}"
                      if q.get("vs_baseline_savings_pct") is not None else "n/a")
                lines.append(
                    f"| `{q['query']}` | {q['hits']} | {q['response_tokens']} "
                    f"| {sv} | {q['latency_ms']['mean']} "
                    f"| {q['latency_ms'].get('p95')} | {cov} "
                    f"| {q.get('rank') if q.get('rank') is not None else '-'} |"
                )
            lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
SCENARIOS_ALL = ("baseline_full", "mdq_bm25", "mdq_grep", "mdq_auto")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--q", action="append", help="Query (repeatable)")
    ap.add_argument("--queries-file",
                    help="File with one query per line (# comments allowed)")
    ap.add_argument("--queries-json",
                    help='JSON array of {"q": "...", "expected_paths": [...]}')
    ap.add_argument("--golden",
                    help="Golden query set JSON (FR-MDQ-01). Enables top-1 / "
                         "top-k accuracy scoring via mdq.golden_eval")
    ap.add_argument("--ignore-golden-paths", action="store_true",
                    help="Ignore the per-query 'paths' filter declared by the "
                         "golden set and search the whole repository "
                         "(FR-MDQ-04 broad condition)")
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
    database_paths: dict[str, str] = {"primary": str(Path(args.db).resolve())}

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
                conn.close()
                return 2
        except Exception as exc:
            print(f"ERROR: index check failed: {exc}", file=sys.stderr)
            conn.close()
            return 2

    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    baseline: dict[str, Any] | None = None
    if "baseline_full" in requested:
        baseline = baseline_full(REPO_ROOT, roots, args.paths)
        print(json.dumps({"event": "baseline_full", "data": baseline},
                         ensure_ascii=False))

    baseline_tokens_total = baseline["tokens"] if baseline else 0

    scenario_results: dict[str, dict[str, Any]] = {}
    # Cache per-strategy connections for mdq_auto.
    _strategy_conns: dict[str, Any] = {}

    def _conn_for_strategy(strategy: str):
        if strategy in _strategy_conns:
            return _strategy_conns[strategy]
        db_p = REPO_ROOT / store.db_path_for(lang="ja-jp", strategy=strategy)
        if not db_p.exists():
            database_paths[f"strategy:{strategy}:fallback"] = database_paths["primary"]
            return None
        database_paths[f"strategy:{strategy}"] = str(db_p.resolve())
        c = store.open_store(db_p, lang="ja-jp")
        _strategy_conns[strategy] = c
        return c

    for scenario in requested:
        if scenario == "baseline_full":
            continue
        if scenario == "mdq_auto":
            # Per-query routing: call query_router and dispatch to the
            # appropriate per-strategy DB. Falls back to the default conn
            # when no per-strategy DB is present.
            try:
                from mdq import query_router as _qr
            except Exception as exc:
                print(f"ERROR: mdq.query_router unavailable: {exc}",
                      file=sys.stderr)
                continue
            available = _qr.discover_available_strategies(REPO_ROOT)
            auto_per_query: list[dict[str, Any]] = []
            all_lat: list[float] = []
            total_tokens = 0
            total_hits_n = 0
            for entry in queries:
                q = entry["q"]
                entry_paths = args.paths if args.ignore_golden_paths else (
                    entry.get("paths") or args.paths
                )
                decision = _qr.classify_query(
                    q,
                    available_strategies=available if available else None,
                    mode="bm25",
                )
                c = _conn_for_strategy(decision.strategy) or conn
                # warmup
                searcher.search(c, q, mode="bm25", top_k=args.top_k,
                                max_tokens=args.max_tokens,
                                path_globs=entry_paths)
                lats: list[float] = []
                hits = []
                for _ in range(args.repeat):
                    t0 = time.perf_counter()
                    hits = searcher.search(
                        c, q, mode="bm25", top_k=args.top_k,
                        max_tokens=args.max_tokens, path_globs=entry_paths,
                    )
                    t1 = time.perf_counter()
                    lats.append((t1 - t0) * 1000.0)
                payload = "\n".join(
                    json.dumps(h.to_dict(), ensure_ascii=False) for h in hits
                )
                resp_tokens = count_tokens(payload)
                total_tokens += resp_tokens
                total_hits_n += len(hits)
                all_lat.extend(lats)
                auto_row: dict[str, Any] = {
                    "query": q,
                    "hits": len(hits),
                    "response_tokens": resp_tokens,
                    "router_strategy": decision.strategy,
                    "router_reason": decision.reason,
                    "router_fallback_used": decision.fallback_used,
                    "database_path": next(
                        (str(Path(row[2]).resolve()) for row in c.execute(
                            "PRAGMA database_list").fetchall() if row[1] == "main"),
                        database_paths["primary"],
                    ),
                    "latency_ms": {
                        "mean": round(statistics.mean(lats), 3),
                        "p50": round(statistics.median(lats), 3),
                        "p95": round(
                            statistics.quantiles(lats, n=20)[18]
                            if len(lats) >= 2 else lats[0], 3),
                    },
                }
                expected_hits = entry.get("expected_hits") or []
                if expected_hits:
                    auto_row["id"] = entry.get("id")
                    auto_row["group"] = entry.get("group")
                    auto_row.update(golden_eval.score_query(
                        hits, expected_hits, args.top_k))
                auto_per_query.append(auto_row)
            avg_tokens = total_tokens / max(1, len(queries))
            savings_pct = (
                round((1.0 - avg_tokens / (baseline_tokens_total / max(1, len(queries)))) * 100.0, 2)
                if baseline_tokens_total else None
            )
            r = {
                "per_query": auto_per_query,
                "avg_response_tokens": round(avg_tokens, 2),
                "avg_vs_baseline_savings_pct": savings_pct,
                "latency_ms_all": {
                    "mean": round(statistics.mean(all_lat), 3) if all_lat else None,
                    "p50": round(statistics.median(all_lat), 3) if all_lat else None,
                    "p95": round(
                        statistics.quantiles(all_lat, n=20)[18]
                        if len(all_lat) >= 2 else all_lat[0], 3) if all_lat else None,
                },
                "total_hits": total_hits_n,
                "scenarios_used": sorted(_strategy_conns.keys()),
                "golden": _golden_summary(auto_per_query, args.top_k),
            }
            scenario_results[scenario] = r
            print(json.dumps({
                "event": "scenario", "name": scenario,
                "avg_response_tokens": r["avg_response_tokens"],
                "avg_savings_pct": r["avg_vs_baseline_savings_pct"],
                "latency_ms_all": r["latency_ms_all"],
                "scenarios_used": r["scenarios_used"],
            }, ensure_ascii=False))
            continue
        mode = "bm25" if scenario == "mdq_bm25" else "grep"
        r = run_search_scenario(
            conn, mode, queries,
            top_k=args.top_k, max_tokens=args.max_tokens,
            repeat=args.repeat, path_globs=args.paths,
            baseline_tokens_total=baseline_tokens_total,
            ignore_golden_paths=args.ignore_golden_paths,
        )
        scenario_results[scenario] = r
        print(json.dumps({
            "event": "scenario",
            "name": scenario,
            "avg_response_tokens": r["avg_response_tokens"],
            "avg_savings_pct": r["avg_vs_baseline_savings_pct"],
            "latency_ms_all": r["latency_ms_all"],
        }, ensure_ascii=False))

    finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = build_report(args, queries, baseline, scenario_results,
                          index_summary, started_at, finished_at,
                          database_paths)

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
        "json_report": _report_path_for_summary(json_path),
        "markdown_report": (_report_path_for_summary(md_path)
                            if md_path else None),
        "tokenizer": TOKENIZER,
        "queries": len(queries),
        "scenarios": list(scenario_results.keys()),
    }
    print(json.dumps(summary_line, ensure_ascii=False))
    for strategy_conn in _strategy_conns.values():
        strategy_conn.close()
    conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
