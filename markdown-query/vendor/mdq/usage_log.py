"""mdq.usage_log — markdown-query Skill 利用ログの追記モジュール。

`.mdq/usage.jsonl` に append-only で 1 コマンド = 1 行の JSON を書き込む。
本モジュールは ``mdq.cli`` の各サブコマンドから呼ばれる。

== レコードスキーマ ==

各行は以下のキーを持つ JSON オブジェクト:

- ``ts``        : ISO8601 UTC タイムスタンプ
- ``command``   : "search" / "get" / "index" / "list" / "stats"
                  (``watch`` は現状未記録。watcher は long-running のため
                  起動/停止イベントの設計が必要で v1 スコープ外。)
- ``args``      : サブコマンド引数の dict。検索クエリ ``q`` もそのまま記録される
                  ため、機微語句（顧客名・内部識別子等）を含む可能性がある。
                  ログはローカル (.hve/) にとどまるが、リポジトリ外転送時は注意。
- ``elapsed_ms``: コマンド実行時間 (ms)
- ``result``    : サブコマンド固有の集計値
                  - search: ``{"hit_count": int, "snippet_chars": int,
                              "source_file_chars": int,
                              "score_top": float (optional),
                              "score_2nd": float (optional),
                              "parent_expanded": int (optional)}``
                  - get   : ``{"found": bool, "body_chars": int}``
                  - index : ``{"files_indexed": int, "files_skipped": int,
                              "chunks_written": int, "pruned_chunks": int}``
                  - list  : ``{"count": int}``
                  - stats : ``{"files": int, "chunks": int}``

  search の ``args`` には以下のキーが追加で記録される（v0.5.0+）:
    - ``strategy``: ユーザー指定値（``auto`` / ``heading`` / ...）
    - ``effective_strategy``: ルーター解決後の実効戦略（``auto`` のときのみ意味を持つ）
    - ``router_reason`` / ``router_rule_id`` / ``router_fallback_used``:
       ``--strategy auto`` の場合のみ記録。判定理由とフォールバック有無。
    - ``with_parent_depth``: ``--with-parent-depth`` 値。
  index の ``args`` には ``strategy`` および ``overlap_paragraphs`` が記録される。
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
from typing import Any, Dict, List, Optional


USAGE_LOG_RELATIVE: str = ".mdq/usage.jsonl"
"""利用ログのリポジトリルート相対パス。"""

_CONTEXT_ENV_VARS = {
    "run_id": "HVE_RUN_ID",
    "workflow_id": "HVE_WORKFLOW_ID",
    "step_id": "HVE_STEP_ID",
    "agent_id": "HVE_AGENT_ID",
}


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds"
    )


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
    """利用ログのフルパスを返す。

    既定はカレントワーキングディレクトリ。``repo_root`` 明示時はそれを使う。
    """
    base = Path(repo_root) if repo_root is not None else Path.cwd()
    return (base / USAGE_LOG_RELATIVE).resolve()


def read_records(
    repo_root: Path,
    *,
    run_id: Optional[str] = None,
    workflow_id: Optional[str] = None,
    since_iso: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """``.mdq/usage.jsonl`` のレコードを読み込んで返す。

    書き込み側（:func:`append_record`）と同じモジュールに置くことで、ログの
    所在と形式の判断を 1 箇所に閉じる。``hve.run_journal`` は本関数へ委譲する。

    Args:
        repo_root: リポジトリルート。``<repo_root>/.mdq/usage.jsonl`` を読む。
        run_id: 指定時は ``record["context"]["run_id"]`` が一致するもののみ返す。
        workflow_id: 指定時は ``record["context"]["workflow_id"]`` が一致するもののみ返す。
        since_iso: 指定時は ``record["ts"] >= since_iso`` のもののみ返す
                   （文字列比較で動作する ISO8601 UTC 前提）。

    Returns:
        条件を満たすレコードのリスト。ファイル不存在時は空リスト。
        パース不能行はスキップする（観測用ログのため例外を投げない）。
    """
    path = _resolve_log_path(repo_root)
    if not path.exists():
        return []
    records: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if since_iso is not None and str(rec.get("ts", "")) < since_iso:
                    continue
                ctx = rec.get("context") or {}
                if run_id is not None and ctx.get("run_id") != run_id:
                    continue
                if workflow_id is not None and ctx.get("workflow_id") != workflow_id:
                    continue
                records.append(rec)
    except OSError:
        return []
    return records


def append_record(
    *,
    command: str,
    args: Dict[str, Any],
    elapsed_ms: int,
    result: Dict[str, Any],
    exit_code: int,
    repo_root: Optional[Path] = None,
) -> Optional[Path]:
    """1 レコードを ``.mdq/usage.jsonl`` に追記する。

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
