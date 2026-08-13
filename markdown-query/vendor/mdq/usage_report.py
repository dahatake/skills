"""markdown-query Skill 利用統計レポート生成スクリプト。

`.mdq/usage.jsonl` と `.mdq/index.sqlite` から 15 指標を集計し、

- ``<repo_root>/.mdq/usage-report/YYYY-MM-DD.json``
- ``<repo_root>/.mdq/usage-report/YYYY-MM-DD.md``
- ``<repo_root>/.mdq/usage-report/latest.json``（コピー）
- ``<repo_root>/.mdq/usage-report/latest.md``（コピー）

の 4 ファイルを書き出す。GUI からの呼び出しと、CLI/cron 等の手動実行の双方で
利用される。レポート本文は人間可読 Markdown、機械可読 JSON の両方を生成する。

== 使い方 ==

::

    python -m mdq.usage_report --repo-root <repo> [--window-days 7]

リポジトリルートが未指定の場合は本モジュールの位置から推定する。
"""

from __future__ import annotations

import argparse
import datetime
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict


# ---------------------------------------------------------------------------
# 出力先
# ---------------------------------------------------------------------------

OUTPUT_DIR_RELATIVE: str = ".mdq/usage-report"


def default_output_dir(repo_root: Path) -> Path:
    """レポートの既定出力先を返す（単一の判断箇所）。

    GUI 表示と生成処理が同じパスを独自に組み立てないよう、本関数を唇一の
    決定経路とする。集計元の ``.mdq/usage.jsonl`` と同じ対象リポジトリ配下に
    置くことで、配布キットを任意のリポジトリへコピーしても無関係な階層を
    作らない（FR-KIT-04）。
    """
    return (Path(repo_root).resolve() / OUTPUT_DIR_RELATIVE).resolve()


def _resolve_repo_root(arg: Path | None) -> Path:
    if arg is not None:
        return Path(arg).resolve()
    # 本モジュールは <repo_root>/mdq/ 直下に存在する
    return Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Markdown レンダラー
# ---------------------------------------------------------------------------

def _fmt(value: Any) -> str:
    if value is None:
        return "（データ不足）"
    if isinstance(value, bool):
        return "はい" if value else "いいえ"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def render_markdown(stats: Dict[str, Any]) -> str:
    """15 指標を人間可読 Markdown に整形する。

    各セクションは「大項目（太字、`##` なし）」+ 2 列テーブル（項目／データ）。
    GUI 側は QTextBrowser.setMarkdown でテーブルレンダリングして表示する。
    """
    def _row(label: str, value: Any) -> str:
        # Markdown table cell: pipes inside content escaped.
        v = _fmt(value).replace("|", "\\|").replace("\n", " ")
        l = label.replace("|", "\\|")
        return f"| {l} | {v} |"

    lines: list[str] = []
    lines.append("# markdown-query Skill 利用統計レポート")
    lines.append("")
    lines.append("対象指標: v1 採用 15 指標 + v1.1 追加 D3 / G4 = 計 17 指標。")
    lines.append("各指標の定義・解釈は "
                 "[users-guide/skills-markdown-query.md]"
                 "(../../../../users-guide/skills-markdown-query.md) を参照。")
    lines.append("")
    lang = stats.get("lang") or "-"
    strategy = stats.get("strategy") or "-"
    meta_rows = [
        ("生成時刻", datetime.datetime.now().isoformat(timespec='seconds')),
        ("集計ウィンドウ", f"直近 {stats.get('window_days', '-')} 日"),
        ("集計開始時刻 (UTC)", stats.get('since_iso', '-')),
        ("レコード数", stats.get('record_count', 0)),
        # NOTE: `.mdq/usage.jsonl` には lang/strategy が記録されていない
        # ため、ここで表示する値は「現在 GUI/CLI で選択中の DB インスタンス」
        # を示すラベルに過ぎず、下記指標の集計値は全 (lang, strategy)
        # 横断であることを明示する（誤読防止）。
        ("Tokenize 言語 (表示中の DB)", lang),
        ("Chunking Strategy (表示中の DB)", strategy),
        ("集計スコープ", "全 (lang, strategy) 横断"),
    ]
    lines.append("| 項目 | データ |")
    lines.append("|:---|---:|")
    for k, v in meta_rows:
        lines.append(_row(k, v))
    lines.append("")
    lines.append("> 各指標の定義・解釈は [users-guide/skills-markdown-query.md]"
                 "(../../../users-guide/skills-markdown-query.md) を参照。")
    lines.append("")

    def _section(title: str, rows: list[tuple[str, Any]]) -> None:
        lines.append(f"**{title}**")
        lines.append("")
        lines.append("| 項目 | データ |")
        lines.append("|:---|---:|")
        for k, v in rows:
            lines.append(_row(k, v))
        lines.append("")

    # ① 基盤・索引
    e1 = stats["E1_index_size"]
    e2 = stats["E2_index_freshness"]
    e5 = stats["E5_pruned_chunks_total"]
    f2 = stats["F2_index_delta_update_ratio"]
    _section("① 基盤・索引", [
        ("E1 索引サイズ - ファイル数", e1.get("files")),
        ("E1 索引サイズ - チャンク数", e1.get("chunks")),
        ("E2 索引鮮度 - 経過秒", e2.get("age_seconds")),
        ("E2 索引鮮度 - DB mtime", e2.get("db_mtime")),
        ("E5 孤児チャンク削除累計 (件)", e5.get("value")),
        ("E5 集計対象 index 実行回数", e5.get("window_records")),
        ("F2 索引差分更新比率", f2.get("value")),
        ("F2 サンプルサイズ", f2.get("sample_size")),
    ])

    # ② 呼び出し量・選択妥当性
    a1 = stats["A1_command_counts"]
    a1_str = (", ".join(f"{k}={v}" for k, v in sorted(a1.items()))
              if a1 else "（呼び出しなし）")
    a2 = stats["A2_calls_per_step"]
    a4 = stats["A4_skill_routing_listed"]
    d3 = stats.get("D3_typical_query_rate") or {}
    per_workflow = d3.get("per_workflow") or {}
    # workflow リストは per_workflow の dict キー（挿入順 = mdq.usage_stats の
    # _D3_TARGET_WORKFLOWS と同順）を SSOT として参照する。
    # 生成スクリプト側でハードコードしない（レビュー No.1 対応）。
    _wf_ids: tuple[str, ...] = tuple(per_workflow.keys())
    # ② セクション基本行（合算 + workflow 横断指標）
    sec2_rows: list[tuple[str, Any]] = [
        ("A1 サブコマンド別呼び出し回数", a1_str),
        ("A2 Step あたり呼び出し回数 (全 Workflow)", a2.get("value")),
        ("A2 総呼び出し回数", a2.get("total_calls")),
        ("A2 distinct Step 数", a2.get("distinct_steps")),
        ("A4 Skill _routing 記載", a4.get("value")),
        ("D1 DO NOT USE FOR 違反 (件)",
         stats.get("D1_donot_use_for_violations")),
        ("D3 典型クエリ出現率 (合算)", d3.get("value")),
        ("D3 合算マッチ件数", d3.get("matched_count")),
        ("D3 合算 search 総件数 (patterns 定義済み workflow のみ)",
         d3.get("total_search")),
    ]
    # workflow 別 D3 行を追加（Q3=C: workflow 別 + 合算）。
    # patterns 未定義 workflow は「出現率」行のみ表示し、空の集計行 3 件を
    # 省略する（レビュー No.6 対応: 冗長削減）。
    for wf in _wf_ids:
        wf_stat = per_workflow.get(wf) or {}
        if not wf_stat.get("per_pattern"):
            # patterns 未定義: 出現率行のみ
            sec2_rows.append((f"D3 {wf} 出現率", wf_stat.get("value")))
            continue
        per_pat = wf_stat.get("per_pattern") or []
        per_pat_str = ", ".join(
            f"{p.get('label', p.get('id', ''))}={p.get('count', 0)}"
            for p in per_pat
        )
        sec2_rows.append((f"D3 {wf} 出現率", wf_stat.get("value")))
        sec2_rows.append((f"D3 {wf} マッチ件数", wf_stat.get("matched_count")))
        sec2_rows.append((f"D3 {wf} search 総件数", wf_stat.get("total_search")))
        sec2_rows.append((f"D3 {wf} パターン別マッチ数", per_pat_str))
    _section("② 呼び出し量・選択妥当性", sec2_rows)
    if d3.get("note"):
        lines.append(f"- D3 合算注記: {d3['note']}")
        # 合算 note があり、かつ全 workflow note が同一 "未定義" 系であれば
        # per-workflow note 出力を省略（レビュー No.7 対応: 同義情報の連続出力抑制）。
        skip_per_wf_notes = all(
            (per_workflow.get(wf) or {}).get("note") is not None
            and "未定義" in ((per_workflow.get(wf) or {}).get("note") or "")
            for wf in _wf_ids
        )
    else:
        skip_per_wf_notes = False
    if not skip_per_wf_notes:
        for wf in _wf_ids:
            wf_stat = per_workflow.get(wf) or {}
            if wf_stat.get("note"):
                lines.append(f"- D3 {wf} 注記: {wf_stat['note']}")
    # per_pattern の独立カウント注意書き（patterns 定義済み workflow があれば表示）
    if any((per_workflow.get(wf) or {}).get("per_pattern") for wf in _wf_ids):
        lines.append("- D3 パターン別カウントの読み方: "
                     "各パターンの count は独立集計のため、合計が "
                     "matched_count と一致するとは限らない（1 search が "
                     "複数パターンにマッチする場合あり）。")

    # ③ Context 削減
    b1 = stats["B1_context_reduction_ratio"]
    b2 = stats["B2_arg_averages"]
    b3 = stats["B3_get_search_ratio"]
    _section("③ Context 削減", [
        ("B1 Context 削減率", b1.get("value")),
        ("B1 snippet 文字数合計", b1.get("snippet_chars")),
        ("B1 参照元ファイル文字数合計", b1.get("source_file_chars")),
        ("B2 top_k 平均", b2.get("top_k_avg")),
        ("B2 max_tokens 平均", b2.get("max_tokens_avg")),
        ("B2 snippet_radius 平均", b2.get("snippet_radius_avg")),
        ("B2 サンプルサイズ", b2.get("sample_size")),
        ("B3 get/search 比率", b3.get("value")),
        ("B3 get 回数", b3.get("get_count")),
        ("B3 search 回数", b3.get("search_count")),
    ])

    # ④ 結果品質
    c1 = stats["C1_zero_hit_rate"]
    c2 = stats.get("C2_score_gap_avg") or {}
    c3 = stats["C3_expansion_flag_usage_rate"]
    c4_rows: list[tuple[str, Any]] = [
        ("C1 ヒット 0 件率", c1.get("value")),
        ("C1 zero-hit 回数", c1.get("zero_hit_count")),
        ("C1 search 回数", c1.get("search_count")),
        ("C2 上位 2 件 score 差 (平均)", c2.get("value")),
        ("C2 サンプルサイズ", c2.get("sample_size")),
    ]
    if c2.get("note"):
        c4_rows.append(("C2 注記", c2.get("note")))
    c4_rows.extend([
        ("C3 expansion フラグ使用率", c3.get("value")),
        ("C3 expansion 回数", c3.get("expansion_count")),
        ("C3 search 回数", c3.get("search_count")),
    ])
    _section("④ 結果品質", c4_rows)

    # ⑤ パフォーマンス
    f1 = stats["F1_search_elapsed_ms"]
    f1_rows: list[tuple[str, Any]] = [
        ("F1 search 実行時間 p50 (ms)", f1.get("p50")),
        ("F1 search 実行時間 p95 (ms)", f1.get("p95")),
        ("F1 サンプルサイズ", f1.get("sample_size")),
    ]
    _section("⑤ パフォーマンス", f1_rows)

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 出力 I/O
# ---------------------------------------------------------------------------

def generate_report(repo_root: Path, *, window_days: int = 7,
                     retention_days: int = 90,
                     lang: str = "ja-jp",
                     strategy: str = "heading",
                     today: datetime.date | None = None,
                     output_dir: Path | None = None) -> Dict[str, Path]:
    """レポート 4 ファイルを生成して書き出す。

    Args:
        repo_root: 利用ログ (.mdq/usage.jsonl) と索引が置かれるリポジトリルート。
        window_days: 集計対象期間（日、既定 7）。``.mdq/usage.jsonl`` の
            レコード絞り込みにのみ適用される（G1/G4 が読む state.json は全期間）。
        retention_days: 保持期間（日、既定 90）。これより古い
            ``YYYY-MM-DD.{json,md}`` を削除する。``0`` 以下なら削除しない。
            ``latest.{json,md}`` は常に保持する。
        lang: 索引 DB 選択用言語（``mdq.store.db_path_for`` 互換）。
            既定 ``ja-jp``。E1/E2 が参照する DB ファイル名に反映される。
        strategy: チャンク戦略（同上）。既定 ``heading``。
        today: テスト用に基準日を上書き。
        output_dir: レポート出力先。明示指定がない場合は
            :func:`default_output_dir` の戻り値を使う。

    Returns:
        ``{"json": <path>, "md": <path>, "latest_json": <path>,
        "latest_md": <path>, "pruned": [<path>, ...]}`` 形式の dict。
    """
    from mdq import usage_stats

    repo_root = Path(repo_root).resolve()
    today = today or datetime.date.today()
    if output_dir is None:
        out_dir = default_output_dir(repo_root)
    else:
        out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    stats = usage_stats.aggregate_usage_stats(repo_root, window_days=window_days)
    stats["generated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    stats["lang"] = lang
    stats["strategy"] = strategy

    date_str = today.strftime("%Y-%m-%d")
    json_path = out_dir / f"{date_str}.json"
    md_path = out_dir / f"{date_str}.md"
    latest_json = out_dir / "latest.json"
    latest_md = out_dir / "latest.md"

    json_path.write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path.write_text(render_markdown(stats), encoding="utf-8")
    shutil.copyfile(json_path, latest_json)
    shutil.copyfile(md_path, latest_md)
    pruned = _prune_old_reports(out_dir, today=today,
                                 retention_days=retention_days)
    return {
        "json": json_path,
        "md": md_path,
        "latest_json": latest_json,
        "latest_md": latest_md,
        "pruned": pruned,
    }


def _prune_old_reports(out_dir: Path, *, today: datetime.date,
                        retention_days: int) -> list[Path]:
    """``retention_days`` 日より古い ``YYYY-MM-DD.{json,md}`` を削除する。

    ``latest.*`` は常に保持。``retention_days <= 0`` の場合は何もしない。
    返り値は削除したファイルのパスリスト。
    """
    if retention_days <= 0:
        return []
    import re as _re
    pattern = _re.compile(r"^(\d{4})-(\d{2})-(\d{2})\.(json|md)$")
    cutoff = today - datetime.timedelta(days=retention_days)
    removed: list[Path] = []
    for f in out_dir.iterdir():
        if not f.is_file():
            continue
        m = pattern.match(f.name)
        if not m:
            continue
        try:
            file_date = datetime.date(int(m.group(1)), int(m.group(2)),
                                       int(m.group(3)))
        except ValueError:
            continue
        if file_date < cutoff:
            try:
                f.unlink()
                removed.append(f)
            except OSError:
                pass
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="markdown-query Skill 利用統計レポート生成"
    )
    parser.add_argument("--repo-root", type=Path, default=None,
                        help="リポジトリルート（未指定時はスクリプト位置から推定）")
    parser.add_argument("--window-days", type=int, default=7,
                        help="集計対象期間（日、既定 7）")
    parser.add_argument("--retention-days", type=int, default=90,
                        help="日付付きレポートの保持期間（日、既定 90）。"
                             "0 以下で削除無効化。latest.* は常に保持。")
    parser.add_argument("--lang", choices=["ja-jp", "en-us"], default="ja-jp",
                        help="Tokenize 言語 (既定 ja-jp)。レポートメタに記録される。")
    parser.add_argument("--strategy",
                        choices=["heading", "heading_recursive", "fixed_window"],
                        default="heading",
                        help="Chunking strategy (既定 heading)。レポートメタに記録される。")
    args = parser.parse_args(argv)
    repo_root = _resolve_repo_root(args.repo_root)
    paths = generate_report(repo_root, window_days=args.window_days,
                             retention_days=args.retention_days,
                             lang=args.lang, strategy=args.strategy)
    print(json.dumps({k: (str(v) if not isinstance(v, list)
                          else [str(x) for x in v])
                      for k, v in paths.items()},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
