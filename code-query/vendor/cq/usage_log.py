"""cq.usage_log — code-query Skill 利用ログの追記モジュール（FR-CQ-14）。

`.cq/usage.jsonl` に append-only で 1 コマンド = 1 行の JSON を書き込む。
本モジュールは ``cq.cli`` の各サブコマンドから呼ばれる。

== レコードスキーマ ==

各行は以下のキーを持つ JSON オブジェクト:

- ``ts``        : ISO8601 UTC タイムスタンプ
- ``command``   : "index" / "stats" / "search" / "def" / "get" / "refs" / "trace" / "map"
                  (``watch`` は long-running のため記録しない。)
- ``args``      : サブコマンド引数の dict。検索クエリもそのまま記録されるため、
                  機微語句を含む可能性がある。ログはローカル (.cq/) にとどまるが、
                  リポジトリ外へ持ち出す際は注意する。
- ``elapsed_ms``: コマンド実行時間 (ms)
- ``result``    : サブコマンド固有の集計値
- ``context``   : Orchestrator から伝播された任意フィールド
                  ``{"run_id"?, "workflow_id"?, "step_id"?, "agent_id"?}``
- ``exit_code`` : 終了コード (int)

捏造禁止: 値が取得不能な場合はキー自体を省略する（``None`` を入れない）。
"""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional


USAGE_LOG_RELATIVE: str = ".cq/usage.jsonl"
"""利用ログのリポジトリルート相対パス。`mdq` の `.mdq/usage.jsonl` とは別ファイル。"""

_CONTEXT_ENV_VARS = {
    "run_id": "HVE_RUN_ID",
    "workflow_id": "HVE_WORKFLOW_ID",
    "step_id": "HVE_STEP_ID",
    "agent_id": "HVE_AGENT_ID",
}


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _read_context_from_env() -> Dict[str, str]:
    """Orchestrator が子プロセスへ伝播した文脈を環境変数から読む。

    値が空文字 / 未設定の項目はキーごと省略する（捏造防止）。
    """
    ctx: Dict[str, str] = {}
    for key, env_name in _CONTEXT_ENV_VARS.items():
        val = os.environ.get(env_name, "")
        if val:
            ctx[key] = val
    return ctx


def _resolve_log_path(repo_root: Optional[Path] = None) -> Path:
    base = Path(repo_root) if repo_root is not None else Path.cwd()
    return (base / USAGE_LOG_RELATIVE).resolve()


def append_record(
    *,
    command: str,
    args: Dict[str, Any],
    elapsed_ms: int,
    result: Dict[str, Any],
    exit_code: int,
    repo_root: Optional[Path] = None,
) -> Optional[Path]:
    """1 レコードを ``.cq/usage.jsonl`` に追記する。

    書き込みに失敗しても呼び出し元の処理を中断しないこと（本ログは観測用で、
    Skill 本体の動作を阻害してはならない）。失敗時は None を返す。
    成功時は書き込み先のパスを返す。
    """
    rec: Dict[str, Any] = {
        "ts": _utc_now_iso(),
        "command": str(command),
        "args": dict(args or {}),
        "elapsed_ms": int(elapsed_ms),
        "result": dict(result or {}),
        "exit_code": int(exit_code),
    }
    ctx = _read_context_from_env()
    if ctx:
        rec["context"] = ctx

    try:
        path = _resolve_log_path(repo_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return path
    except Exception:
        # 観測用ログ。Skill 本体動作を止めないため握り潰す。
        return None
