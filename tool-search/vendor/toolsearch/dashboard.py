"""HVE Tool Search — ダッシュボード（FR-TS-10）。

`stats.aggregate` が返す `DashboardSnapshot` をテキスト / JSON / 自己完結 HTML へ描画する。

**捏造しない**: 算出不能な指標は 0 や推定値で埋めず `NO_DATA` を表示する。
**外部へ出ない**: HTML は CDN・外部フォント・リモート画像・スクリプトを一切参照しない。
"""

from __future__ import annotations

import html
import json
import os
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Sequence

from .stats import DashboardSnapshot, aggregate, default_events_path, load_events
from .usage import default_usage_path, load_usage

NO_DATA = "データ不足"

DEFAULT_WIDTH = 78
DEFAULT_INTERVAL_SEC = 2.0

# 端末描画で使う棒グラフの文字。等幅前提。
_BAR_FULL = "█"
_BAR_EMPTY = "·"


def build_dashboard(
    *,
    events_path: Path | str | None = None,
    usage_path: Path | str | None = None,
    since: str | None = None,
    top: int = 10,
) -> DashboardSnapshot:
    """2 つのストアを読んで集計する。どちらも無ければ空のスナップショットになる。"""
    return aggregate(
        load_events(events_path if events_path is not None else default_events_path()),
        usage_records=load_usage(usage_path if usage_path is not None else default_usage_path()),
        since=since,
        top=top,
    )


# ---------------------------------------------------------------------------
# 表示用の整形（None を 0 にしない）
# ---------------------------------------------------------------------------


def _num(value: Any, *, suffix: str = "", digits: int = 1) -> str:
    if value is None:
        return NO_DATA
    if isinstance(value, float):
        return f"{value:.{digits}f}{suffix}"
    return f"{value}{suffix}"


def _pct(value: float | None) -> str:
    return NO_DATA if value is None else f"{value * 100:.1f}%"


def _token_reduction_text(snapshot: DashboardSnapshot) -> str:
    """遅延公開が一度も発火していないときは削減率として出さない（FR-TS-10）。"""
    if not snapshot.token_reduction_valid:
        return "無効（遅延公開が発火していない）"
    return _pct(snapshot.token_reduction)


def _bar(value: float | None, *, width: int) -> str:
    if value is None or width <= 0:
        return ""
    filled = max(0, min(width, round(value * width)))
    return _BAR_FULL * filled + _BAR_EMPTY * (width - filled)


# 端末は全角を 2 桁で描くため、コードポイント数で揃えると日本語ラベルの桁がずれる。
def _cols(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _cols(text))


def _clip(text: str, width: int) -> str:
    """表示桁で切り詰める。全角が境界にかかる場合はその文字ごと落とす。"""
    if _cols(text) <= width:
        return text
    kept: list[str] = []
    used = 0
    for ch in text:
        char_width = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + char_width > width:
            break
        kept.append(ch)
        used += char_width
    return "".join(kept)


# ---------------------------------------------------------------------------
# テキスト
# ---------------------------------------------------------------------------


def _section(title: str, width: int) -> str:
    body = f"─ {title} "
    return body + "─" * max(0, width - _cols(body))


def _kv_lines(pairs: Sequence[tuple[str, str]], width: int) -> list[str]:
    label_width = max((_cols(label) for label, _ in pairs), default=0)
    return [_clip(f"  {_pad(label, label_width)} : {value}", width) for label, value in pairs]


def _ranked_lines(rows: Sequence[tuple[str, int]], width: int) -> list[str]:
    """件数は `aggregate(top=...)` で既に絞られているのでここでは切らない。"""
    if not rows:
        return [f"  {NO_DATA}"]
    top_count = max(count for _, count in rows) or 1
    label_width = min(34, max(_cols(label) for label, _ in rows))
    bar_width = max(0, width - label_width - 10)
    lines = []
    for label, count in rows:
        clipped = _clip(label, label_width)
        if clipped != label:
            clipped = _clip(label, label_width - 1) + "…"
        lines.append(
            _clip(
                f"  {_pad(clipped, label_width)} {str(count).rjust(4)} "
                f"{_bar(count / top_count, width=bar_width)}",
                width,
            )
        )
    return lines


def render_text(snapshot: DashboardSnapshot, *, width: int = DEFAULT_WIDTH) -> str:
    """端末向けダッシュボード。ANSI エスケープを含めない（ログへ貼れるように）。"""
    catalog = snapshot.catalog
    lines: list[str] = [
        _clip("HVE Tool Search ダッシュボード", width),
        _clip(f"生成: {snapshot.generated_at}", width),
        _clip(
            f"期間: {snapshot.first_event_at or NO_DATA} 〜 {snapshot.last_event_at or NO_DATA}",
            width,
        ),
        "",
        _section("検索の起動状況", width),
        *_kv_lines(
            [
                ("検索回数", str(snapshot.queries)),
                ("うち miss", str(snapshot.misses)),
                ("ヒット率", _pct(snapshot.hit_rate)),
                ("セッション数", str(snapshot.sessions)),
                ("run 数", str(snapshot.runs)),
                ("遅延公開が不活性だった割合", _pct(snapshot.deferral_inactive_rate)),
            ],
            width,
        ),
        "",
        _section("応答", width),
        *_kv_lines(
            [
                ("平均返却件数", _num(snapshot.avg_hits, digits=2)),
                ("平均トップスコア", _num(snapshot.avg_top_score, digits=3)),
                ("レイテンシ p50", _num(snapshot.latency_p50_ms, suffix=" ms")),
                ("レイテンシ p95", _num(snapshot.latency_p95_ms, suffix=" ms")),
                ("レイテンシ 最大", _num(snapshot.latency_max_ms, suffix=" ms")),
            ],
            width,
        ),
        "",
        _section("カタログ構成", width),
        *_kv_lines(
            [
                ("総数", str(catalog.total) if catalog.total else NO_DATA),
                ("pinned / searchable", f"{catalog.pinned} / {catalog.searchable}"),
                ("除外 (dropped)", str(catalog.dropped)),
                ("deferred", str(catalog.deferred)),
                ("mcp / native / skill", f"{catalog.mcp} / {catalog.native} / {catalog.skill}"),
            ],
            width,
        ),
        "",
        _section("コンテキストコスト", width),
        *_kv_lines(
            [
                ("全定義前置き (baseline)", _num(snapshot.baseline_tokens, suffix=" tokens")),
                ("実公開 (exposed)", _num(snapshot.exposed_tokens, suffix=" tokens")),
                ("トークン削減", _token_reduction_text(snapshot)),
            ],
            width,
        ),
        "",
        _section("発見の質", width),
        *_kv_lines([("採用率 (発見 → 実呼び出し)", _pct(snapshot.adoption_rate))], width),
        "",
        "  よく使われたクエリ",
        *_ranked_lines(snapshot.top_queries, width),
        "",
        "  よく返されたツール",
        *_ranked_lines(snapshot.top_hit_tools, width),
        "",
        "  ヒットしなかったクエリ（検索語彙の改善候補）",
        *_ranked_lines(snapshot.top_miss_queries, width),
        "",
        "  一度も返されていないツール",
        *(
            [_clip(f"  {', '.join(snapshot.never_hit_tools[:8])}", width)]
            if snapshot.never_hit_tools
            else [f"  {NO_DATA}"]
        ),
        "",
        _section("自動 pin のウォームアップ (FR-TS-07)", width),
    ]

    if snapshot.autopin_progress:
        for progress in snapshot.autopin_progress[:8]:
            ratio = min(1.0, progress.sessions / progress.warmup_sessions)
            promoted = ", ".join(progress.promoted) if progress.promoted else "未昇格"
            lines.append(
                _clip(
                    f"  {_pad(_clip(progress.scope, 22), 22)} "
                    f"{str(progress.sessions).rjust(3)}/{progress.warmup_sessions} "
                    f"{_bar(ratio, width=10)} {promoted}",
                    width,
                )
            )
    else:
        lines.append(f"  {NO_DATA}")

    if snapshot.warnings:
        lines += ["", _section("警告", width)]
        lines += [_clip(f"  [{count}] {message}", width) for message, count in snapshot.warnings[:5]]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def render_json(snapshot: DashboardSnapshot) -> str:
    return json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# HTML（自己完結・外部参照なし）
# ---------------------------------------------------------------------------

_CSS = """
:root { color-scheme: light dark; }
body { font-family: system-ui, sans-serif; margin: 0; padding: 24px; line-height: 1.6; }
h1 { font-size: 20px; margin: 0 0 4px; }
.meta { color: #6b7280; font-size: 13px; margin-bottom: 20px; }
.cards { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 24px; }
.card { border: 1px solid #d1d5db; border-radius: 8px; padding: 12px 16px; min-width: 150px; }
.card .label { font-size: 12px; color: #6b7280; }
.card .value { font-size: 22px; font-weight: 600; }
.card .value.nodata { font-size: 14px; font-weight: 400; color: #9ca3af; }
h2 { font-size: 15px; margin: 24px 0 8px; border-bottom: 1px solid #d1d5db; padding-bottom: 4px; }
table { border-collapse: collapse; width: 100%; max-width: 760px; font-size: 13px; }
th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid #e5e7eb; }
th { color: #6b7280; font-weight: 500; white-space: nowrap; }
td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.nodata { color: #9ca3af; }
.bar { fill: #2563eb; }
.track { fill: #e5e7eb; }
""".strip()


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _card(label: str, value: str) -> str:
    css = "value nodata" if value == NO_DATA else "value"
    return f'<div class="card"><div class="label">{_e(label)}</div><div class="{css}">{_e(value)}</div></div>'


def _svg_bar(ratio: float | None, *, width: int = 120, height: int = 10) -> str:
    """インライン SVG の横棒。外部リソースを使わない。"""
    if ratio is None:
        return f'<span class="nodata">{_e(NO_DATA)}</span>'
    filled = max(0, min(width, round(ratio * width)))
    return (
        f'<svg width="{width}" height="{height}" role="img" aria-label="{ratio * 100:.0f}%">'
        f'<rect class="track" x="0" y="0" width="{width}" height="{height}" rx="2"/>'
        f'<rect class="bar" x="0" y="0" width="{filled}" height="{height}" rx="2"/>'
        "</svg>"
    )


def _ranked_table(
    rows: Sequence[tuple[str, int]],
    headers: tuple[str, str],
) -> str:
    if not rows:
        return f'<p class="nodata">{_e(NO_DATA)}</p>'
    top_count = max(count for _, count in rows) or 1
    body = "".join(
        f"<tr><td>{_e(label)}</td><td class=\"num\">{count}</td>"
        f"<td>{_svg_bar(count / top_count)}</td></tr>"
        for label, count in rows
    )
    return (
        f"<table><thead><tr><th>{_e(headers[0])}</th><th>{_e(headers[1])}</th><th></th></tr></thead>"
        f"<tbody>{body}</tbody></table>"
    )


def render_html(snapshot: DashboardSnapshot) -> str:
    """自己完結 HTML。スクリプト・外部参照・リモート画像を持たない。"""
    catalog = snapshot.catalog
    cards = "".join(
        _card(label, value)
        for label, value in (
            ("検索回数", str(snapshot.queries)),
            ("ヒット率", _pct(snapshot.hit_rate)),
            ("採用率", _pct(snapshot.adoption_rate)),
            ("トークン削減", _token_reduction_text(snapshot)),
            ("レイテンシ p95", _num(snapshot.latency_p95_ms, suffix=" ms")),
            ("カタログ", str(catalog.total) if catalog.total else NO_DATA),
        )
    )

    catalog_rows = "".join(
        f'<tr><td>{_e(label)}</td><td class="num">{value}</td></tr>'
        for label, value in (
            ("総数", catalog.total),
            ("pinned", catalog.pinned),
            ("searchable", catalog.searchable),
            ("dropped", catalog.dropped),
            ("deferred", catalog.deferred),
            ("mcp", catalog.mcp),
            ("native", catalog.native),
            ("skill", catalog.skill),
        )
    )

    if snapshot.autopin_progress:
        autopin_rows = "".join(
            f"<tr><td>{_e(p.scope)}</td>"
            f'<td class="num">{p.sessions}/{p.warmup_sessions}</td>'
            f"<td>{_svg_bar(min(1.0, p.sessions / p.warmup_sessions))}</td>"
            f"<td>{_e(', '.join(p.promoted) if p.promoted else '未昇格')}</td></tr>"
            for p in snapshot.autopin_progress
        )
        autopin = (
            "<table><thead><tr><th>workflow:step</th><th>セッション</th><th></th>"
            f"<th>昇格</th></tr></thead><tbody>{autopin_rows}</tbody></table>"
        )
    else:
        autopin = f'<p class="nodata">{_e(NO_DATA)}</p>'

    never_hit = (
        "<p>" + ", ".join(_e(name) for name in snapshot.never_hit_tools) + "</p>"
        if snapshot.never_hit_tools
        else f'<p class="nodata">{_e(NO_DATA)}</p>'
    )

    warnings = (
        "<ul>" + "".join(f"<li>[{count}] {_e(msg)}</li>" for msg, count in snapshot.warnings) + "</ul>"
        if snapshot.warnings
        else f'<p class="nodata">{_e(NO_DATA)}</p>'
    )

    latency_rows = "".join(
        f'<tr><td>{_e(label)}</td><td class="num">{_e(_num(value, suffix=" ms"))}</td></tr>'
        for label, value in (
            ("p50", snapshot.latency_p50_ms),
            ("p95", snapshot.latency_p95_ms),
            ("最大", snapshot.latency_max_ms),
        )
    )

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HVE Tool Search ダッシュボード</title>
<style>{_CSS}</style>
</head>
<body>
<h1>HVE Tool Search ダッシュボード</h1>
<p class="meta">生成 {_e(snapshot.generated_at)} ／ 期間 {_e(snapshot.first_event_at or NO_DATA)} 〜 {_e(snapshot.last_event_at or NO_DATA)} ／ run {snapshot.runs} ／ セッション {snapshot.sessions}</p>
<div class="cards">{cards}</div>

<h2>カタログ構成</h2>
<table><thead><tr><th>項目</th><th>件数</th></tr></thead><tbody>{catalog_rows}</tbody></table>

<h2>コンテキストコスト</h2>
<table><thead><tr><th>項目</th><th>推定トークン</th></tr></thead><tbody>
<tr><td>全定義前置き (baseline)</td><td class="num">{_e(_num(snapshot.baseline_tokens))}</td></tr>
<tr><td>実公開 (exposed)</td><td class="num">{_e(_num(snapshot.exposed_tokens))}</td></tr>
<tr><td>削減率</td><td class="num">{_e(_token_reduction_text(snapshot))}</td></tr>
</tbody></table>

<h2>レイテンシ</h2>
<table><thead><tr><th>分位</th><th>値</th></tr></thead><tbody>{latency_rows}</tbody></table>

<h2>よく使われたクエリ</h2>
{_ranked_table(snapshot.top_queries, ("クエリ", "回数"))}

<h2>よく返されたツール</h2>
{_ranked_table(snapshot.top_hit_tools, ("ツール", "回数"))}

<h2>ヒットしなかったクエリ</h2>
{_ranked_table(snapshot.top_miss_queries, ("クエリ", "回数"))}

<h2>実際に呼ばれたツール</h2>
{_ranked_table(snapshot.top_called_tools, ("ツール ID", "回数"))}

<h2>Step 別の検索回数</h2>
{_ranked_table(snapshot.queries_by_scope, ("workflow:step", "回数"))}

<h2>一度も返されていないツール</h2>
{never_hit}

<h2>自動 pin のウォームアップ</h2>
{autopin}

<h2>警告</h2>
{warnings}
</body>
</html>
"""


# ---------------------------------------------------------------------------
# ライブ更新
# ---------------------------------------------------------------------------


def _clear_screen(stream) -> None:
    if stream.isatty():
        stream.write("\x1b[2J\x1b[H")


def run_live(
    *,
    events_path: Path | str | None = None,
    usage_path: Path | str | None = None,
    since: str | None = None,
    interval: float = DEFAULT_INTERVAL_SEC,
    width: int = DEFAULT_WIDTH,
    stream=None,
    iterations: int | None = None,
) -> int:
    """一定間隔で再集計して描画し直す。Ctrl+C で終了する。

    ``iterations`` はテスト用の打ち切り回数（既定 ``None`` = 無限）。
    """
    out = stream if stream is not None else sys.stdout
    count = 0
    try:
        while iterations is None or count < iterations:
            snapshot = build_dashboard(
                events_path=events_path, usage_path=usage_path, since=since
            )
            _clear_screen(out)
            out.write(render_text(snapshot, width=width) + "\n")
            out.write(f"\n(更新間隔 {interval:.0f}s / Ctrl+C で終了)\n")
            out.flush()
            count += 1
            if iterations is not None and count >= iterations:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        out.write("\n")
    return 0


def terminal_width(default: int = DEFAULT_WIDTH) -> int:
    try:
        return max(40, min(160, os.get_terminal_size().columns))
    except OSError:
        return default
