"""mdq.usage_stats — markdown-query Skill 利用ログから 17 指標を集計する。

入力: ``.mdq/usage.jsonl`` （``mdq.usage_log`` が追記）と
``.mdq/index.sqlite`` 索引統計（``mdq.gui.index_service.get_index_stats`` 経由）。

出力: 17 指標を含む dict（JSON シリアライズ可能）。

== 採用指標 ==

v1（15 項目）:
  グループ① 基盤・索引: E1 / E2 / E5 / F2
  グループ② 呼び出し量・選択妥当性: A1 / A2 / A4 / D1
    （A2 は全 Workflow を対象に Step あたり呼び出し回数を集計する。
     v1.2 以降キー名は ``A2_calls_per_step``。
     v1.1 以前は ``A2_aad_web_calls_per_step`` で aad-web 限定だった）
  グループ③ Context 削減: B1 / B2 / B3
  グループ④ 結果品質: C1 / C2 / C3
  グループ⑤ パフォーマンス: F1

v1.1 追加（2 項目）:
  - D3: 典型クエリ出現率（template/typical-queries.json 参照）
    - v1.1: aad-web 限定（キー名 ``D3_aad_web_typical_query_rate``）
    - v1.2 以降: 全 workflow 横断（aad-web / asdw-web / adfd / adfdv）。
      キー名は ``D3_typical_query_rate`` に変更（BREAKING CHANGE）。
      合算値は patterns 定義済み workflow のみを分母にした micro-average。
      ``per_workflow`` 配下に workflow 別の内訳を保持する。

v2.0 追加（2 項目, schema_version=2）:
  - H1_auto_strategy_distribution: ``--strategy auto`` 実行時に
    query_router が選択した strategy / reason の分布、フォールバック率。
  - H2_parent_expansion_rate: ``--with-parent-depth`` / ``--include-parent``
    を伴う search レコードのうち、実際に parent chunk が返却された比率。

== 重要な制約 ==

- **window_days の適用範囲**: ``aggregate_usage_stats(window_days=N)`` は
  ``.mdq/usage.jsonl`` のレコード絞り込みに適用される。
- **B1 の負値**: 複数 search 間で参照元ファイルの重複排除を行わないため、
  理論上 B1 が負になり得る。集計側で 0 以上にクリップし、note で明示する。

捏造禁止: データが存在しない / 算出不能な指標は ``None`` でなく
``{"value": None, "note": "サンプル不足"}`` のように明示する。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from mdq import usage_log


# ロギング期間の既定ウィンドウ
_DEFAULT_WINDOW_DAYS = 7


def _percentile(values: List[float], pct: float) -> Optional[float]:
    """単純な分位点計算（昇順ソート + 線形補間）。値なしなら None。"""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * pct
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return float(s[f])
    return float(s[f] + (s[c] - s[f]) * (k - f))


def _paths_violate_donot_use(paths: Iterable[str]) -> bool:
    """``--paths`` に ``knowledge/D\\d\\d`` 形式が含まれる場合 True。

    SKILL.md の DO NOT USE FOR: knowledge/D01-D21 lookup は
    ``knowledge-lookup`` Skill の責務であるため、これを mdq で検索するのは違反。
    ``knowledge/Data-foo.md`` 等を誘発しないよう ``D`` の後に 2 桁数字を
    要求する厳格マッチとする。
    """
    import re as _re
    # 文字列単体が誤って渡された場合のガード（1 文字ずつ反復されると
    # 全て不一致になりシグナルとして意味を失うため）。
    if isinstance(paths, str):
        paths = [paths]
    pattern = _re.compile(r"^(?:\./)?knowledge/D\d{2}\b")
    for p in paths or []:
        if pattern.match(str(p)):
            return True
    return False


def _check_skill_routing_listed(repo_root: Path) -> bool:
    """`.github/skills/_routing/README.md` に ``markdown-query`` の文字列があるか。"""
    p = repo_root / ".github" / "skills" / "_routing" / "README.md"
    if not p.exists():
        return False
    try:
        return "markdown-query" in p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def _load_typical_queries(repo_root: Path,
                           workflow_id: str = "aad-web") -> List[Dict[str, Any]]:
    """``template/typical-queries.json`` から指定ワークフローのエントリを読む。

    ファイル不存在・JSON 不正・該当 workflow_id 無しの場合は空リスト。
    ``schema_version`` が 1 と一致しない場合も空リストを返す（将来の
    破壊的変更で旧 hve が誤読するのを防ぐ）。
    各エントリは ``{"id": str, "label": str, "patterns": [str, ...]}``。
    """
    p = repo_root / "template" / "typical-queries.json"
    if not p.exists():
        return []
    try:
        import json as _json
        data = _json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if data.get("schema_version") != 1:
        return []
    wf = (data.get("workflows") or {}).get(workflow_id) or []
    if not isinstance(wf, list):
        return []
    out: List[Dict[str, Any]] = []
    for e in wf:
        if not isinstance(e, dict):
            continue
        patterns = e.get("patterns") or []
        if not isinstance(patterns, list):
            continue
        out.append({
            "id": str(e.get("id", "")),
            "label": str(e.get("label", "")),
            "patterns": [str(x) for x in patterns],
        })
    return out


# ---------------------------------------------------------------------------
# 各グループの集計関数
# ---------------------------------------------------------------------------

def _group_invocation(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """A1 / A2 / D1 を集計する（A4 は別関数）。

    A2 は全 Workflow を対象に、step_id を持つレコード件数 ÷ distinct(step_id)
    を算出する（旧 v1.1 では workflow_id == "aad-web" 限定だった）。
    """
    a1_counts: Dict[str, int] = {}
    a2_calls = 0
    a2_steps: set[str] = set()
    d1_violations = 0
    for rec in records:
        cmd = str(rec.get("command", ""))
        if cmd:
            a1_counts[cmd] = a1_counts.get(cmd, 0) + 1
        sid = (rec.get("context") or {}).get("step_id")
        if sid:
            a2_calls += 1
            a2_steps.add(str(sid))
        if cmd == "search":
            paths = (rec.get("args") or {}).get("paths") or []
            if _paths_violate_donot_use(paths):
                d1_violations += 1
    a2_per_step: Optional[float]
    if a2_steps:
        a2_per_step = round(a2_calls / len(a2_steps), 2)
    else:
        a2_per_step = None
    return {
        "A1_command_counts": a1_counts,
        "A2_calls_per_step": {
            "value": a2_per_step,
            "total_calls": a2_calls,
            "distinct_steps": len(a2_steps),
            "note": "サンプル不足" if a2_per_step is None else None,
        },
        "D1_donot_use_for_violations": d1_violations,
    }


def _group_context_reduction(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """B1 / B2 / B3 を集計する。"""
    search_records = [r for r in records if r.get("command") == "search"]
    get_count = sum(1 for r in records if r.get("command") == "get")
    search_count = len(search_records)

    sum_snippet = 0
    sum_source = 0
    top_ks: List[int] = []
    max_tokens: List[int] = []
    snippet_radius: List[int] = []
    for r in search_records:
        res = r.get("result") or {}
        sum_snippet += int(res.get("snippet_chars", 0) or 0)
        sum_source += int(res.get("source_file_chars", 0) or 0)
        args = r.get("args") or {}
        if "top_k" in args:
            try:
                top_ks.append(int(args["top_k"]))
            except (TypeError, ValueError):
                pass
        if "max_tokens" in args:
            try:
                max_tokens.append(int(args["max_tokens"]))
            except (TypeError, ValueError):
                pass
        if "snippet_radius" in args:
            try:
                snippet_radius.append(int(args["snippet_radius"]))
            except (TypeError, ValueError):
                pass

    if sum_source > 0:
        raw_ratio = 1.0 - (sum_snippet / sum_source)
        # 集計ロジック上、複数 search 間で参照元ファイルの重複排除をしない
        # （cli 側の seen_files は 1 search 内スコープ）ため、snippet 合計が
        # source 合計を上回って B1 が負になる場合がある。表示の混乱を避け
        # 0 にクリップし、その旨を note で明示する。
        clipped = False
        if raw_ratio < 0:
            raw_ratio = 0.0
            clipped = True
        b1_ratio = round(raw_ratio, 4)
        b1 = {
            "value": b1_ratio,
            "snippet_chars": sum_snippet,
            "source_file_chars": sum_source,
            "note": ("集計上 snippet 合計が参照元合計を上回ったため 0 にクリップ"
                     "（複数 search 間でファイル重複排除を行わない仕様）")
                    if clipped else None,
        }
    else:
        b1 = {"value": None, "snippet_chars": sum_snippet,
              "source_file_chars": sum_source, "note": "サンプル不足"}

    def _avg(xs: List[int]) -> Optional[float]:
        return round(sum(xs) / len(xs), 2) if xs else None

    b2 = {
        "top_k_avg": _avg(top_ks),
        "max_tokens_avg": _avg(max_tokens),
        "snippet_radius_avg": _avg(snippet_radius),
        "sample_size": len(search_records),
    }
    if search_count > 0:
        b3 = {
            "value": round(get_count / search_count, 4),
            "get_count": get_count,
            "search_count": search_count,
            "note": None,
        }
    else:
        b3 = {
            "value": None,
            "get_count": get_count,
            "search_count": 0,
            "note": "search 呼び出しなし",
        }
    return {
        "B1_context_reduction_ratio": b1,
        "B2_arg_averages": b2,
        "B3_get_search_ratio": b3,
    }


def _group_quality(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """C1 / C2 / C3 を集計する。"""
    search_records = [r for r in records if r.get("command") == "search"]
    if not search_records:
        return {
            "C1_zero_hit_rate": {"value": None, "note": "search 呼び出しなし"},
            "C2_score_gap_avg": {"value": None, "note": "search 呼び出しなし"},
            "C3_expansion_flag_usage_rate": {"value": None,
                                             "note": "search 呼び出しなし"},
        }
    zero = 0
    expansion = 0
    gaps: List[float] = []
    for r in search_records:
        res = r.get("result") or {}
        if int(res.get("hit_count", 0) or 0) == 0:
            zero += 1
        args = r.get("args") or {}
        if (args.get("include_parent")
                or int(args.get("expand_neighbors", 0) or 0) > 0
                or args.get("merge_parts")):
            expansion += 1
        top = res.get("score_top")
        second = res.get("score_2nd")
        if top is not None and second is not None:
            try:
                gaps.append(float(top) - float(second))
            except (TypeError, ValueError):
                pass
    n = len(search_records)
    if gaps:
        c2 = {
            "value": round(sum(gaps) / len(gaps), 4),
            "sample_size": len(gaps),
            "note": None,
        }
    else:
        c2 = {
            "value": None,
            "sample_size": 0,
            "note": "ヒット 2 件以上の search が無いため算出不能",
        }
    return {
        "C1_zero_hit_rate": {
            "value": round(zero / n, 4),
            "zero_hit_count": zero,
            "search_count": n,
            "note": None,
        },
        "C2_score_gap_avg": c2,
        "C3_expansion_flag_usage_rate": {
            "value": round(expansion / n, 4),
            "expansion_count": expansion,
            "search_count": n,
            "note": None,
        },
    }


def _group_performance(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """F1 / F2 を集計する。"""
    search_ms = [int(r.get("elapsed_ms", 0))
                 for r in records if r.get("command") == "search"]
    f1_p50 = _percentile([float(v) for v in search_ms], 0.5)
    f1_p95 = _percentile([float(v) for v in search_ms], 0.95)

    index_records = [r for r in records if r.get("command") == "index"]
    ratios: List[float] = []
    for r in index_records:
        res = r.get("result") or {}
        idx = int(res.get("files_indexed", 0) or 0)
        skp = int(res.get("files_skipped", 0) or 0)
        total = idx + skp
        if total > 0:
            ratios.append(idx / total)
    if ratios:
        f2 = {
            "value": round(sum(ratios) / len(ratios), 4),
            "sample_size": len(ratios),
            "note": None,
        }
    else:
        f2 = {"value": None, "sample_size": 0, "note": "index 呼び出しなし"}

    return {
        "F1_search_elapsed_ms": {
            "p50": f1_p50,
            "p95": f1_p95,
            "sample_size": len(search_ms),
            "note": None if search_ms else "search 呼び出しなし",
        },
        "F2_index_delta_update_ratio": f2,
    }


# D3 対象ワークフロー（hve/orchestrator.py の _ARCH_FILTER_WORKFLOWS と同じ集合）。
# 増減があれば双方を同時に更新すること。
_D3_TARGET_WORKFLOWS: tuple[str, ...] = ("aad-web", "asdw-web", "adfd", "adfdv")


def _compute_workflow_typical_query(
    queries: List[Dict[str, Any]],
    search_records: List[Dict[str, Any]],
    *,
    workflow_id: str,
) -> Dict[str, Any]:
    """単一ワークフロー分の D3 集計結果を返す。

    queries が空（patterns 未定義）or search_records が 0 件の場合は
    value=None + note を返し、捏造値を入れない。
    patterns 未定義時は ``total_search`` を 0 として返す（集計対象外で
    あることを表すため。レビュー No.2 / No.4 対応）。
    ``per_pattern`` は queries 定義済み workflow でのみ意味を持つため、
    queries 未定義時は空配列で揃える（スキーマ一貫性、レビュー No.4 対応）。
    """
    import re as _re
    if not queries:
        # patterns 未定義: search_records があっても集計対象外。total_search=0 で
        # JSON 消費者が「マッチ率 0%」と誤読しないようにする。
        return {
            "value": None,
            "matched_count": 0,
            "total_search": 0,
            "per_pattern": [],
            "note": f"template/typical-queries.json に {workflow_id} エントリ未定義",
        }
    total = len(search_records)
    if total == 0:
        return {
            "value": None,
            "matched_count": 0,
            "total_search": 0,
            "per_pattern": [{"id": q["id"], "label": q["label"], "count": 0}
                            for q in queries],
            "note": f"{workflow_id} の search レコードなし",
        }
    # コンパイル
    compiled: List[tuple[Dict[str, Any], List[Any]]] = []
    for q in queries:
        regs = []
        for p in q["patterns"]:
            try:
                regs.append(_re.compile(p, _re.IGNORECASE))
            except _re.error:
                pass
        compiled.append((q, regs))
    # matched_count と per_pattern を 1 ループで同時集計（レビュー No.3, No.5 対応）。
    matched_count = 0
    per_pattern_counts: List[int] = [0] * len(compiled)
    for r in search_records:
        qstr = str((r.get("args") or {}).get("q", ""))
        hit_any = False
        for idx, (_q, regs) in enumerate(compiled):
            if any(rg.search(qstr) for rg in regs):
                per_pattern_counts[idx] += 1
                hit_any = True
        if hit_any:
            matched_count += 1
    per_pattern = [
        {"id": q["id"], "label": q["label"], "count": per_pattern_counts[i]}
        for i, (q, _regs) in enumerate(compiled)
    ]
    return {
        "value": round(matched_count / total, 4),
        "matched_count": matched_count,
        "total_search": total,
        "per_pattern": per_pattern,
        "note": None,
    }


def _group_typical_queries(repo_root: Path,
                            records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """D3 典型クエリ出現率を全 workflow 横断で集計する。

    v1.2 以降:
      - 出力キーは ``D3_typical_query_rate`` （旧 ``D3_aad_web_typical_query_rate``）。
      - ``per_workflow`` に各 workflow の集計結果を格納。
      - 合算 ``value`` は micro-average: 全 workflow のマッチ件数合計 /
        patterns 定義済み workflow の search 総件数合計（Q6=A 採用）。
      - patterns 未定義 workflow も ``per_workflow`` に行を出すが、
        合算の分母には含めない（サンプル不足を 0% と誤読させないため）。
    """
    # 各 workflow ごとの search レコード抽出
    per_workflow: Dict[str, Dict[str, Any]] = {}
    agg_matched = 0
    agg_total = 0
    any_defined = False
    for wf in _D3_TARGET_WORKFLOWS:
        wf_records = [r for r in records
                      if r.get("command") == "search"
                      and (r.get("context") or {}).get("workflow_id") == wf]
        queries = _load_typical_queries(repo_root, wf)
        wf_stat = _compute_workflow_typical_query(
            queries, wf_records, workflow_id=wf
        )
        per_workflow[wf] = wf_stat
        if queries:
            any_defined = True
            agg_matched += int(wf_stat.get("matched_count") or 0)
            agg_total += int(wf_stat.get("total_search") or 0)

    if not any_defined:
        return {
            "D3_typical_query_rate": {
                "value": None,
                "matched_count": 0,
                "total_search": 0,
                "per_workflow": per_workflow,
                "note": "template/typical-queries.json に対象 workflow の patterns 未定義",
            }
        }
    if agg_total == 0:
        return {
            "D3_typical_query_rate": {
                "value": None,
                "matched_count": 0,
                "total_search": 0,
                "per_workflow": per_workflow,
                "note": "patterns 定義済み workflow の search レコードなし",
            }
        }
    return {
        "D3_typical_query_rate": {
            "value": round(agg_matched / agg_total, 4),
            "matched_count": agg_matched,
            "total_search": agg_total,
            "per_workflow": per_workflow,
            "note": None,
        }
    }


def _group_routing(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """H1 / H2 を集計する。

    - H1 (auto strategy distribution): ``--strategy auto`` で実行された
      search レコードの ``router_reason`` 別出現比率と ``effective_strategy``
      別比率、フォールバック率を返す。auto 以外の search は分母に含めない。
    - H2 (parent expansion rate): ``with_parent_depth >= 1`` または
      ``include_parent=True`` のレコードのうち、実際に parent が
      返却されたものの比率を返す。

    捏造禁止: サンプルが 0 件の場合は ``value: None`` + ``note`` を返す。
    """
    search_recs = [r for r in records if r.get("command") == "search"]

    # H1 ---------------------------------------------------------------
    auto_recs = [
        r for r in search_recs
        if str((r.get("args") or {}).get("strategy", "")) == "auto"
    ]
    h1: Dict[str, Any]
    if not auto_recs:
        h1 = {
            "value": None,
            "total_auto_search": 0,
            "note": "auto strategy search レコードが 0 件",
        }
    else:
        reason_counts: Dict[str, int] = {}
        effective_counts: Dict[str, int] = {}
        fallback_count = 0
        for r in auto_recs:
            a = r.get("args") or {}
            reason = str(a.get("router_reason", "")) or "unknown"
            eff = str(a.get("effective_strategy", "")) or "unknown"
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
            effective_counts[eff] = effective_counts.get(eff, 0) + 1
            if a.get("router_fallback_used"):
                fallback_count += 1
        total = len(auto_recs)
        h1 = {
            "total_auto_search": total,
            "fallback_count": fallback_count,
            "fallback_rate": round(fallback_count / total, 4) if total else None,
            "by_reason": {
                k: {"count": v, "rate": round(v / total, 4)}
                for k, v in sorted(reason_counts.items())
            },
            "by_effective_strategy": {
                k: {"count": v, "rate": round(v / total, 4)}
                for k, v in sorted(effective_counts.items())
            },
            "note": None,
        }

    # H2 ---------------------------------------------------------------
    parent_recs = [
        r for r in search_recs
        if (r.get("args") or {}).get("include_parent")
        or int((r.get("args") or {}).get("with_parent_depth", 0) or 0) >= 1
    ]
    h2: Dict[str, Any]
    if not parent_recs:
        h2 = {
            "value": None,
            "total_parent_requests": 0,
            "note": "parent 展開を要求した search レコードが 0 件",
        }
    else:
        expanded_count = 0
        expanded_hits = 0
        total_hits = 0
        for r in parent_recs:
            res = r.get("result") or {}
            exp = int(res.get("parent_expanded", 0) or 0)
            hits = int(res.get("hit_count", 0) or 0)
            if exp > 0:
                expanded_count += 1
            expanded_hits += exp
            total_hits += hits
        total_reqs = len(parent_recs)
        h2 = {
            "total_parent_requests": total_reqs,
            "request_expanded_rate": round(
                expanded_count / total_reqs, 4
            ) if total_reqs else None,
            "hit_expanded_rate": round(
                expanded_hits / total_hits, 4
            ) if total_hits else None,
            "expanded_hits": expanded_hits,
            "total_hits_in_parent_requests": total_hits,
            "note": None,
        }

    return {
        "H1_auto_strategy_distribution": h1,
        "H2_parent_expansion_rate": h2,
    }


def _group_index(repo_root: Path, records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """E1 / E2 / E5 を集計する（E1/E2 は索引 DB の最新状態に依存）。"""
    # 索引統計は GUI 既存サービスを利用する（重複実装を避ける）
    e5_pruned = 0
    for r in records:
        if r.get("command") == "index":
            e5_pruned += int((r.get("result") or {}).get("pruned_chunks", 0) or 0)
    try:
        from mdq.gui import index_service as mdq_index_service
        idx = mdq_index_service.get_index_stats(repo_root)
        e1 = {"files": int(idx.get("files", 0)),
              "chunks": int(idx.get("chunks", 0)),
              "note": None}
        # 索引鮮度: db_mtime ISO 文字列を秒に変換
        # mdq_index_service._file_mtime_iso は naive ISO 文字列を返す。
        # ローカル TZ を補完してから比較するため、サマータイム境界や
        # システム TZ 変更直後は数時間程度の誤差を含む点に留意（v2 で
        # _file_mtime_iso 側を UTC ISO に変更する想定）。
        import datetime as _dt
        mtime_str = str(idx.get("db_mtime", ""))
        try:
            mtime = _dt.datetime.fromisoformat(mtime_str)
            if mtime.tzinfo is None:
                # naive ISO を OS ローカル TZ として補完
                mtime = mtime.astimezone()
            now = _dt.datetime.now(mtime.tzinfo)
            e2 = {"age_seconds": int((now - mtime).total_seconds()),
                  "db_mtime": mtime_str,
                  "note": ("naive ISO mtime をローカル TZ として解釈。"
                           "TZ 変更直後は誤差を含み得る。")}
        except (ValueError, TypeError):
            e2 = {"age_seconds": None, "db_mtime": mtime_str,
                  "note": "索引未作成または mtime 解析失敗"}
    except Exception as exc:  # pragma: no cover - defensive
        e1 = {"files": None, "chunks": None,
              "note": f"索引取得失敗: {exc}"}
        e2 = {"age_seconds": None, "db_mtime": None,
              "note": f"索引取得失敗: {exc}"}
    return {
        "E1_index_size": e1,
        "E2_index_freshness": e2,
        "E5_pruned_chunks_total": {
            "value": e5_pruned,
            "window_records": sum(1 for r in records
                                  if r.get("command") == "index"),
            "note": None,
        },
    }


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------

def aggregate_usage_stats(
    repo_root: Path,
    *,
    window_days: int = _DEFAULT_WINDOW_DAYS,
    records: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """``.mdq/usage.jsonl`` を読み込み、15 指標を集計して返す。

    Args:
        repo_root: リポジトリルート。
        window_days: 集計対象期間（日）。``since`` 時刻はこの値から算出。
        records: テスト等で外部から差し込み可能。``None`` のときファイルから読込。

    Returns:
        v1 採用 15 指標を含む dict（JSON シリアライズ可能）。
    """
    import datetime as _dt

    repo_root = Path(repo_root)
    since = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=window_days)
    since_iso = since.isoformat(timespec="seconds")

    if records is None:
        records = usage_log.read_records(
            repo_root, since_iso=since_iso
        )

    result: Dict[str, Any] = {
        "schema_version": 2,
        "window_days": window_days,
        "since_iso": since_iso,
        "record_count": len(records),
    }
    result.update(_group_index(repo_root, records))
    result.update(_group_invocation(records))
    result["A4_skill_routing_listed"] = {
        "value": _check_skill_routing_listed(repo_root),
        "note": None,
    }
    result.update(_group_context_reduction(records))
    result.update(_group_quality(records))
    result.update(_group_performance(records))
    result.update(_group_typical_queries(repo_root, records))
    result.update(_group_routing(records))
    return result


__all__ = ["aggregate_usage_stats"]
