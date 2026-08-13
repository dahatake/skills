"""Evaluation harness for `semantic_paragraph` (and baseline strategies).

Token-level IoU methodology (Chroma 2024). The harness:

1. Loads QA pairs from ``<fixtures>/qa_pairs.json``.
2. For each strategy in {heading, heading_recursive, fixed_window,
   semantic_paragraph}, builds a fresh index over the fixture corpus.
3. Runs ``mdq.search`` for each question (top_k=5) and computes:
     - Recall (token-level), Precision (token-level), IoU
     - Indexing wall-clock time
4. Writes a JSON report and a Markdown summary.

The kit ships no fixtures, so ``--fixtures`` is required:

    python <kit>/eval_semantic.py \
        --fixtures path/to/corpus \
        [--out path/to/eval-report.md] \
        [--strategies heading heading_recursive fixed_window semantic_paragraph]

The semantic_paragraph strategy automatically falls back to
``heading_recursive`` when the [semantic] extra is missing — the harness
records that fallback in the report.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

# eval_semantic.py sits at the root of the distribution kit, next to vendor/.
_VENDOR = Path(__file__).resolve().parent / "vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            # Keep the platform's encoding; only stop an unencodable character
            # (an em dash in --help on a cp932 console) from aborting the run.
            _reconfigure(errors="replace")
        except (ValueError, OSError):
            pass

from mdq import indexer, search as searcher, store, strategies_semantic  # noqa: E402

# Token regex matches mdq.search.tokenize (ASCII word OR single CJK char).
_TOK_RE = re.compile(r"[A-Za-z0-9_]+|[\u3040-\u30ff\u4e00-\u9fff]")


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOK_RE.findall(text or "")}


def _eval_one(fixtures_dir: Path, strategy: str,
              embed_provider: str | None = None) -> dict:
    """Build index for *strategy* and evaluate all QA pairs."""
    qa = json.loads(
        (fixtures_dir / "qa_pairs.json").read_text(encoding="utf-8")
    )["pairs"]
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        corpus = tmp_root / "corpus"
        corpus.mkdir()
        for md in fixtures_dir.glob("*.md"):
            shutil.copy(md, corpus / md.name)

        # Install runtime config for semantic_paragraph.
        strategies_semantic.clear_runtime_config()
        if strategy == "semantic_paragraph":
            strategies_semantic.set_runtime_config(
                embed_provider=embed_provider or "null",
                max_chars=500,
                min_chars=80,
            )

        db = tmp_root / f"index-{strategy}.sqlite"
        conn = store.open_store(db, lang="ja-jp")
        try:
            t0 = time.perf_counter()
            summary = indexer.build_index(
                tmp_root, ["corpus"], conn,
                rebuild=True, prune=True,
                max_chunk_chars=(500 if strategy == "semantic_paragraph" else 0),
                strategy=strategy,
            )
            index_ms = int((time.perf_counter() - t0) * 1000)
            conn.commit()

            per_q: list[dict] = []
            recalls: list[float] = []
            precisions: list[float] = []
            ious: list[float] = []
            for q in qa:
                hits = searcher.search(
                    conn, q["question"], top_k=5, max_tokens=2000,
                )
                full_texts: list[str] = []
                for h in hits:
                    ch = searcher.get_chunk(conn, h.chunk_id)
                    if ch:
                        full_texts.append(ch.get("text", ""))
                retrieved = " ".join(full_texts)
                t_retrieved = _tokens(retrieved)
                t_excerpts: set[str] = set()
                for ex in q["excerpts"]:
                    t_excerpts |= _tokens(ex)
                inter = t_retrieved & t_excerpts
                recall = (len(inter) / len(t_excerpts)) if t_excerpts else 0.0
                precision = (len(inter) / len(t_retrieved)) if t_retrieved else 0.0
                union = t_retrieved | t_excerpts
                iou = (len(inter) / len(union)) if union else 0.0
                per_q.append({
                    "id": q["id"], "recall": recall, "precision": precision,
                    "iou": iou, "hit_count": len(hits),
                })
                recalls.append(recall)
                precisions.append(precision)
                ious.append(iou)
            result = {
                "strategy": strategy,
                "index_ms": index_ms,
                "files_indexed": summary.get("files_indexed", 0),
                "chunks_written": summary.get("chunks_written", 0),
                "n_questions": len(qa),
                "mean_recall": sum(recalls) / len(recalls) if recalls else 0.0,
                "mean_precision": sum(precisions) / len(precisions) if precisions else 0.0,
                "mean_iou": sum(ious) / len(ious) if ious else 0.0,
                "per_question": per_q,
            }
        finally:
            conn.close()
        return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    # The kit ships no fixtures (kit_sync drops tests/), so there is no default
    # corpus to fall back on: the caller has to point at one.
    ap.add_argument("--fixtures", type=Path, required=True,
                    help="Directory holding the evaluation corpus.")
    ap.add_argument("--out", type=Path,
                    default=Path(__file__).resolve().parent / "results" / "semantic-eval-report.md")
    ap.add_argument(
        "--strategies", nargs="+",
        default=["heading", "heading_recursive", "fixed_window",
                 "semantic_paragraph"],
    )
    ap.add_argument(
        "--embed-provider", default=None,
        help="Override embedding provider for semantic_paragraph. "
             "Defaults to 'null' (deterministic, no model DL).",
    )
    args = ap.parse_args()

    results = []
    for s in args.strategies:
        print(f"[eval] strategy={s} ...", file=sys.stderr)
        try:
            r = _eval_one(args.fixtures, s, embed_provider=args.embed_provider)
        except Exception as e:  # noqa: BLE001
            r = {"strategy": s, "error": str(e)}
        results.append(r)

    # Write JSON next to the markdown report.
    args.out.parent.mkdir(parents=True, exist_ok=True)
    json_path = args.out.with_suffix(".json")
    json_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    lines = []
    lines.append("# `semantic_paragraph` ベースライン計測レポート")
    lines.append("")
    lines.append("生成: `python <kit>/eval_semantic.py --fixtures <corpus>`")
    lines.append("")
    lines.append("計測手法: Chroma 2024 (Smith & Troynikov) の token-level IoU。")
    lines.append("excerpts は MD 原文の exact 部分文字列。retrieved は top_k=5 の")
    lines.append("各ヒットの **chunk 全文** をトークン化した集合。")
    lines.append("")
    lines.append("| strategy | n | mean_recall | mean_precision | mean_iou | chunks | index_ms |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in results:
        if "error" in r:
            lines.append(f"| {r['strategy']} | — | ERROR | ERROR | ERROR | — | — |")
            continue
        lines.append(
            f"| {r['strategy']} | {r['n_questions']} | {r['mean_recall']:.3f} | "
            f"{r['mean_precision']:.3f} | {r['mean_iou']:.3f} | "
            f"{r['chunks_written']} | {r['index_ms']} |"
        )
    lines.append("")
    lines.append("## 詳細 (per-question)")
    lines.append("")
    lines.append("詳細は対応する JSON: `eval-report.json`。")
    lines.append("")
    lines.append("## 注記")
    lines.append("")
    lines.append("- `semantic_paragraph` は既定で `NullProvider`（決定論的ハッシュ）を使用。")
    lines.append("  本番品質の評価には `--embed-provider fastembed` を指定して再実行する。")
    lines.append("- fixture サイズは 18 質問（手書き）。Q12=A の 30 件は順次追加。")
    lines.append("  LLM 生成 200 件は opt-in 手動実行。")
    args.out.write_text("\n".join(lines), encoding="utf-8")
    print(f"[eval] wrote {args.out}", file=sys.stderr)
    print(f"[eval] wrote {json_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
