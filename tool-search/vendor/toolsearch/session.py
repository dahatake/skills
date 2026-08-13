"""HVE Tool Search — セッションへの配線ヘルパー（FR-TS-01 / 06 / 07）。

`hve/runner.py` 側の差分を小さく保つため、`create_session` へ渡すツール列と
`ToolSearchContext` の組み立てをここへ閉じ込める。

**FR-MODEL-04 との関係**: `SDKConfig.tool_search`（bool）は SDK 組み込みのツール検索を
有効化する設定で、本モジュールはその意味を変えない。`SDKConfig.tool_search_ranking` が
``"hve"`` のときだけランキング実装を差し替える。``tool_search`` が無効なら SDK は
``tool_search_tool`` を呼ばないため、差し替えても何も起こらない。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .metatool import ToolSearchContext, build_tool_search_tool
from .policy import PolicyError, ToolSearchPolicy
from .skill_catalog import (
    SkillDescriptor,
    build_skill_entries,
    build_skill_tools,
    discover_skills,
    skill_manifest_pins,
)
from .usage import auto_pins, load_usage, record_usage

RANKING_SDK = "sdk"
RANKING_HVE = "hve"
VALID_RANKING_MODES: tuple[str, ...] = (RANKING_SDK, RANKING_HVE)


def is_ranking_override_enabled(config: Any) -> bool:
    """HVE ランキングへ差し替えるべきか。

    ``tool_search`` が無効なら SDK が ``tool_search_tool`` を呼ばないので差し替えない。
    """
    if not getattr(config, "tool_search", False):
        return False
    return str(getattr(config, "tool_search_ranking", RANKING_SDK)) == RANKING_HVE


def default_skill_roots(repo_root: Path | str) -> tuple[Path, ...]:
    """リポジトリ内 Skill とユーザースコープの外部 Skill ルート。"""
    root = Path(repo_root)
    return (
        root / ".github" / "skills",
        Path.home() / ".agents" / "skills",
        Path.home() / ".copilot" / "skills",
    )


def load_skill_manifest(repo_root: Path | str) -> Mapping[str, Any]:
    path = Path(repo_root) / "hve" / "skill_manifest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def build_session_toolset(
    config: Any,
    *,
    repo_root: Path | str,
    workflow_id: str | None = None,
    step_id: str | None = None,
    skill_roots: Sequence[Path | str] | None = None,
    on_event: Callable[[str, Mapping[str, Any]], None] | None = None,
    usage_path: Path | str | None = None,
    enabled: bool = True,
) -> tuple[list[Any], ToolSearchContext | None]:
    """``create_session(tools=...)`` へ渡すツール列と、検索コンテキストを組み立てる。

    差し替えが無効なとき、``enabled=False``（呼び出し側のゲート）のとき、
    または `policy.json` が壊れているときは ``([], None)`` を返す
    （検索は SDK 既定のまま動き続ける）。ポリシー不正で Step を落とさない。
    """
    if not enabled or not is_ranking_override_enabled(config):
        return [], None

    try:
        policy = ToolSearchPolicy.load(repo_root=repo_root)
    except PolicyError:
        return [], None

    roots = tuple(skill_roots) if skill_roots is not None else default_skill_roots(repo_root)
    skills: tuple[SkillDescriptor, ...] = discover_skills(roots)

    manifest_pins = skill_manifest_pins(load_skill_manifest(repo_root), workflow_id, step_id)
    promoted = auto_pins(load_usage(usage_path), workflow_id, step_id)

    context = ToolSearchContext(
        policy=policy,
        skill_entries=build_skill_entries(
            skills, pin_for=policy.pin_for, search_text_for=policy.search_text_for
        ),
        manifest_pins=manifest_pins,
        auto_pins=promoted,
        excluded_tools=tuple(getattr(config, "excluded_tools", ()) or ()),
        workflow_id=workflow_id,
        step_id=step_id,
        on_event=on_event,
    )

    core = {
        skill.name
        for skill in skills
        if manifest_pins.get(skill.entry_id) == "always" or policy.pin_for(skill.entry_id) == "always"
    }
    tools = [build_tool_search_tool(context), *build_skill_tools(skills, core_skill_names=core)]
    return tools, context


def record_session_usage(
    called_tool_ids: Sequence[str],
    *,
    session_id: str,
    workflow_id: str | None,
    step_id: str | None,
    usage_path: Path | str | None = None,
) -> int:
    """セッション終了時に呼ぶ。自動 pin（FR-TS-07）の学習材料になる。"""
    if not called_tool_ids or not workflow_id or not step_id:
        return 0
    return record_usage(
        called_tool_ids,
        session_id=session_id,
        workflow_id=workflow_id,
        step_id=step_id,
        path=usage_path,
    )


def resolve_called_tool_ids(
    context: ToolSearchContext | None,
    called_tool_names: Sequence[str],
) -> list[str]:
    """呼ばれたモデル向けツール名を ``ToolEntry.id`` へ解決する。

    カタログに無い名前（SDK 組み込みツール等）は落とす。名前だけで推測して
    id を組み立てない（MCP サーバー間で名前が衝突しうるため）。
    """
    if context is None:
        return []
    resolved: list[str] = []
    for name in called_tool_names:
        entry_id = context.name_to_id.get(str(name))
        if entry_id and entry_id not in resolved:
            resolved.append(entry_id)
    return resolved
