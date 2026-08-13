"""HVE Tool Search — 利用履歴に基づく自動 pin（FR-TS-07）。

Foundry Toolbox の auto-pin は *per user* だが、HVE は **workflow × step 単位の決定論**にする。
HVE のセッションは step ごとに役割が固定されており、step 単位のほうが公開されるツール集合が
安定し、prompt cache の prefix が壊れないため。

同一の履歴に対して常に同一の pin 集合を同一順序で返す（乱数・現在時刻に依存しない）。
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

# ウォームアップ: この session 数に達するまでは静的 pin だけを使う。
DEFAULT_WARMUP_SESSIONS = 20

# 昇格させる上位件数。
DEFAULT_TOP_N = 3

# 失効: 直近この session 数だけを集計対象にする（古い利用傾向を落とす）。
DEFAULT_WINDOW_SESSIONS = 50

# イベントログと利用履歴を置くリポジトリ相対ディレクトリ（FR-TS-07 / FR-TS-09）。
LOG_DIRNAME = ".toolsearch"


def default_usage_path(repo_root: Path | str | None = None) -> Path:
    """既定はリポジトリスコープの履歴ファイル。``HVE_TOOLSEARCH_USAGE`` で差し替えられる。"""
    override = os.environ.get("HVE_TOOLSEARCH_USAGE")
    if override:
        return Path(override)
    base = Path(repo_root) if repo_root is not None else Path.cwd()
    return base / LOG_DIRNAME / "usage.jsonl"


@dataclass(frozen=True)
class UsageRecord:
    session_id: str
    workflow_id: str
    step_id: str
    tool_id: str
    # 後から追加したフィールド。旧レコードは空のまま読む（履歴を捨てない）。
    ts: str = ""

    @property
    def scope(self) -> str:
        return f"{self.workflow_id}:{self.step_id}"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def record_usage(
    tool_ids: Iterable[str],
    *,
    session_id: str,
    workflow_id: str,
    step_id: str,
    path: Path | str | None = None,
) -> int:
    """利用されたツールを追記する。書けなくても例外にしない（記録は best-effort）。"""
    target = Path(path) if path is not None else default_usage_path()
    now = _utc_now_iso()
    lines = [
        json.dumps(
            {
                "ts": now,
                "session_id": session_id,
                "workflow_id": workflow_id,
                "step_id": step_id,
                "tool_id": tool_id,
            },
            ensure_ascii=False,
        )
        for tool_id in dict.fromkeys(tool_ids)
    ]
    if not lines:
        return 0
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError:
        return 0
    return len(lines)


def load_usage(path: Path | str | None = None) -> tuple[UsageRecord, ...]:
    """履歴を読み込む。壊れた行は黙って捨てる（記録は best-effort のため）。"""
    target = Path(path) if path is not None else default_usage_path()
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return ()
    records: list[UsageRecord] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
            records.append(
                UsageRecord(
                    session_id=str(raw["session_id"]),
                    workflow_id=str(raw["workflow_id"]),
                    step_id=str(raw["step_id"]),
                    tool_id=str(raw["tool_id"]),
                    ts=str(raw.get("ts", "")),
                )
            )
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    return tuple(records)


def session_count(records: Sequence[UsageRecord], workflow_id: str, step_id: str) -> int:
    scope = f"{workflow_id}:{step_id}"
    return len({r.session_id for r in records if r.scope == scope})


def auto_pins(
    records: Sequence[UsageRecord],
    workflow_id: str | None,
    step_id: str | None,
    *,
    warmup_sessions: int = DEFAULT_WARMUP_SESSIONS,
    top_n: int = DEFAULT_TOP_N,
    window_sessions: int = DEFAULT_WINDOW_SESSIONS,
) -> tuple[str, ...]:
    """昇格させるツール ID を決定論的に返す。ウォームアップ未満なら空。"""
    if not workflow_id or not step_id:
        return ()
    scope = f"{workflow_id}:{step_id}"
    scoped = [r for r in records if r.scope == scope]
    if not scoped:
        return ()

    # 出現順を保った session 一覧の末尾 window_sessions 件だけを集計対象にする（失効）。
    ordered_sessions = list(dict.fromkeys(r.session_id for r in scoped))
    if len(ordered_sessions) < warmup_sessions:
        return ()
    recent = set(ordered_sessions[-window_sessions:])

    counts = Counter(r.tool_id for r in scoped if r.session_id in recent)
    if not counts:
        return ()
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return tuple(tool_id for tool_id, _ in ranked[:top_n])
