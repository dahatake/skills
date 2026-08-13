"""Tool Search — 配布キット用の CLI 入口。

本ファイルは配布時に `vendor/toolsearch/cli.py` として同梱される。相対 import は
その配置を前提としており、このディレクトリ（`engine/`）単体では解決しない。

上流リポジトリでは `hve toolsearch dashboard` が同じ処理を提供している
（`hve/__main__.py` の `_cmd_toolsearch`）。他リポジトリには HVE 本体を
持ち込まないため、同じ関数群を呼ぶ薄い入口だけをここに置く。

集計・描画・評価のロジックは `dashboard.py` / `stats.py` / `eval.py` が
単独で所有しており、本ファイルは argparse と終了コードしか持たない。

    python -m toolsearch dashboard
    python -m toolsearch dashboard --json
    python -m toolsearch dashboard --html tool-search.html
    python -m toolsearch skills
    python -m toolsearch policy
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from . import dashboard as _dashboard
from . import eval as _eval
from .policy import PolicyError, ToolSearchPolicy
from .skill_catalog import build_skill_entries, discover_skills


def _skill_roots(repo_root: Path) -> tuple[Path, ...]:
    """`session.default_skill_roots` と同じ規則。"""
    return (
        repo_root / ".github" / "skills",
        Path.home() / ".agents" / "skills",
        Path.home() / ".copilot" / "skills",
    )


def _cmd_dashboard(args: argparse.Namespace) -> int:
    if args.follow:
        return _dashboard.run_live(
            events_path=args.events,
            usage_path=args.usage,
            since=args.since,
            interval=float(args.interval),
            width=_dashboard.terminal_width(),
        )

    snapshot = _dashboard.build_dashboard(
        events_path=args.events,
        usage_path=args.usage,
        since=args.since,
        top=int(args.top),
    )

    if args.html:
        target = Path(args.html)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_dashboard.render_html(snapshot), encoding="utf-8")
        except OSError as exc:
            print(f"HTML を書き出せませんでした: {exc}", file=sys.stderr)
            return 1
        print(str(target))

    if args.json:
        print(_dashboard.render_json(snapshot))
    elif not args.html:
        print(_dashboard.render_text(snapshot, width=_dashboard.terminal_width()))

    if snapshot.queries == 0:
        print(
            "\nイベントがまだありません。Copilot SDK セッションへ配線し、"
            "tool_search_tool の差し替えを有効にすると収集が始まります。",
            file=sys.stderr,
        )
    return 0


def _cmd_skills(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    skills = discover_skills(_skill_roots(repo_root))
    if not skills:
        print(f"SKILL.md が見つかりませんでした: {repo_root / '.github' / 'skills'}")
        return 1
    for skill in sorted(skills, key=lambda s: s.name):
        head = skill.description.split("。")[0][:90]
        print(f"{skill.entry_id}\n    {head}")
    print(f"\n{len(skills)} 件")
    return 0


def _cmd_policy(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    path = Path(args.path) if args.path else ToolSearchPolicy.default_path(repo_root)
    try:
        policy = ToolSearchPolicy.load(path)
    except PolicyError as exc:
        print(f"policy 不正: {exc}", file=sys.stderr)
        return 1
    print(f"policy      : {path}")
    print(f"version     : {policy.version}")
    print(f"limit       : {policy.limit} (max {policy.max_limit})")
    print(f"tau         : {policy.tau}")
    print(f"pins        : {len(policy.pins)} 件")
    print(f"search text : {len(policy.additional_search_text)} 件")
    print(f"step 上書き : {len(policy.step_overrides)} 件")
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    """`.github/skills` から作ったカタログを golden クエリで評価する。

    ライブカタログ（SDK の ``available_tools``）はセッション中しか手に入らない。
    オフラインで検証できるのは Skill 由来のエントリだけなので、対象をそこに限る。
    """
    try:
        policy = ToolSearchPolicy.load(
            Path(args.policy) if args.policy else None,
            repo_root=Path(args.repo_root).resolve(),
        )
    except PolicyError as exc:
        print(f"policy 不正: {exc}", file=sys.stderr)
        return 1

    golden_path = Path(args.golden) if args.golden else None
    try:
        golden = _eval.load_golden(golden_path)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        print(f"golden クエリを読めません: {exc}", file=sys.stderr)
        return 1
    if not golden:
        print("golden クエリが空です。", file=sys.stderr)
        return 1

    repo_root = Path(args.repo_root).resolve()
    skills = discover_skills(_skill_roots(repo_root))
    entries = build_skill_entries(
        skills, pin_for=policy.pin_for, search_text_for=policy.search_text_for
    )
    if not entries:
        print(f"SKILL.md が見つかりませんでした: {repo_root / '.github' / 'skills'}", file=sys.stderr)
        return 1

    report = _eval.evaluate(
        entries, golden, field_weights=policy.field_weights, limit=args.limit
    )
    pinned = [e for e in entries if policy.pin_for(e.id) == "always"]
    print(f"catalog     : {len(entries)} entries ({len(pinned)} pinned)")
    print(_eval.format_report(report, _eval.token_report(entries, pinned)))
    return 0 if not report.misses or not args.fail_on_miss else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="toolsearch", description=__doc__)
    sub = parser.add_subparsers(dest="command")

    board = sub.add_parser("dashboard", help="収集済みイベントからダッシュボードを描画する")
    board.add_argument("--events", default=None,
                       help="イベントログ（既定: <repo-root>/.toolsearch/events.jsonl、HVE_TOOLSEARCH_EVENTS）")
    board.add_argument("--usage", default=None,
                       help="利用履歴（既定: <repo-root>/.toolsearch/usage.jsonl、HVE_TOOLSEARCH_USAGE）")
    board.add_argument("--since", default=None, help="この ISO8601 (UTC) 以降だけ集計する")
    board.add_argument("--top", type=int, default=10, help="上位一覧の件数（既定: 10）")
    board.add_argument("--json", action="store_true", help="JSON で出力する")
    board.add_argument("--html", default=None, help="自己完結 HTML を書き出す（外部接続なし）")
    mode = board.add_mutually_exclusive_group()
    mode.add_argument("--follow", action="store_true", help="一定間隔で更新する（Ctrl+C で終了）")
    mode.add_argument("--once", action="store_true", help="1 回だけ描画する（既定）")
    board.add_argument("--interval", type=float, default=2.0, help="--follow の更新間隔秒")
    board.set_defaults(handler=_cmd_dashboard)

    skills = sub.add_parser("skills", help="検索対象になる Skill を列挙する")
    skills.add_argument("--repo-root", default=".", help="リポジトリのルート（既定: カレント）")
    skills.set_defaults(handler=_cmd_skills)

    policy = sub.add_parser("policy", help="policy.json を読み込んで妥当性を確認する")
    policy.add_argument("--repo-root", default=".", help="リポジトリのルート（既定: カレント）")
    policy.add_argument(
        "--path", default=None,
        help="policy.json のパス（既定: <repo-root>/.toolsearch/policy.json があればそれ、"
             "無ければ同梱物）",
    )
    policy.set_defaults(handler=_cmd_policy)

    evaluate = sub.add_parser(
        "eval", help="golden クエリで Skill カタログの Recall@k / MRR / トークン削減率を測る"
    )
    evaluate.add_argument("--repo-root", default=".", help="リポジトリのルート（既定: カレント）")
    evaluate.add_argument(
        "--golden", default=None,
        help="golden クエリ JSON（既定: 同梱の golden-tool-queries.json。"
             "上流のツール構成向けなので導入先では差し替えること）",
    )
    evaluate.add_argument(
        "--policy", default=None,
        help="policy.json のパス（既定: <repo-root>/.toolsearch/policy.json があればそれ、"
             "無ければ同梱物）",
    )
    evaluate.add_argument("--limit", type=int, default=10, help="順位を見る上位件数（既定: 10）")
    evaluate.add_argument(
        "--fail-on-miss", action="store_true", help="1 件でも miss があれば exit 1"
    )
    evaluate.set_defaults(handler=_cmd_eval)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # Skill の description には全角ダッシュ等が入る。cp932 コンソールでも落とさない。
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):
            pass

    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    return int(handler(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
