"""HVE Tool Search — Skill をツールカタログへ合流させる（FR-TS-06）。

Skill は SDK の ``ToolInvocation.available_tools`` に現れない。そのため HVE 側で
各 Skill を ``define_tool`` によるツールとして登録し、MCP / native と同じカタログで
ランキングできるようにする。これにより **普段使わない Skill でも必要な場面で発見できる**。

``disabled_skills`` による一括無効化を long-tail Skill の唯一の手段にしてはならない
（無効化された Skill は検索でも発見できなくなるため）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .types import (
    SKILL_SERVER,
    SKILL_TOOL_PREFIX,
    ToolEntry,
)

# handler が返す SKILL.md 本文の上限。超過分は末尾に案内を付けて切り詰める。
MAX_SKILL_BODY_CHARS = 20_000

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
_NAME_RE = re.compile(r"^name:\s*(.+?)\s*$", re.MULTILINE)
# `description: >` の折り返しブロックと 1 行形式の両方を拾う。
_DESCRIPTION_BLOCK_RE = re.compile(
    r"^description:\s*(?:[>|][-+]?\s*\n((?:[ \t]+.*\n?)+)|(.+))", re.MULTILINE
)


@dataclass(frozen=True)
class SkillDescriptor:
    """1 つの SKILL.md を表す。"""

    name: str
    description: str
    path: Path
    category: str = ""

    @property
    def tool_name(self) -> str:
        return f"{SKILL_TOOL_PREFIX}{self.name}"

    @property
    def entry_id(self) -> str:
        return ToolEntry.make_id("skill", SKILL_SERVER, self.tool_name)


def parse_skill_file(path: Path) -> SkillDescriptor | None:
    """SKILL.md の frontmatter から name / description を取り出す。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None
    front = match.group(1)
    name_match = _NAME_RE.search(front)
    if not name_match:
        return None
    desc_match = _DESCRIPTION_BLOCK_RE.search(front)
    if desc_match and desc_match.group(1):
        description = " ".join(line.strip() for line in desc_match.group(1).splitlines() if line.strip())
    elif desc_match:
        description = desc_match.group(2).strip()
    else:
        description = ""
    return SkillDescriptor(name=name_match.group(1), description=description, path=path)


def discover_skills(roots: Iterable[Path | str]) -> tuple[SkillDescriptor, ...]:
    """Skill ルート群から SKILL.md を収集する。同名は先勝ち（リポジトリ優先）。"""
    found: dict[str, SkillDescriptor] = {}
    for root in roots:
        base = Path(root)
        if not base.is_dir():
            continue
        for skill_md in sorted(base.rglob("SKILL.md")):
            descriptor = parse_skill_file(skill_md)
            if descriptor is None or descriptor.name in found:
                continue
            relative = skill_md.relative_to(base).parent.as_posix()
            category = relative.rsplit("/", 1)[0] if "/" in relative else ""
            found[descriptor.name] = SkillDescriptor(
                name=descriptor.name,
                description=descriptor.description,
                path=skill_md,
                category=category,
            )
    return tuple(found.values())


def build_skill_entries(
    skills: Sequence[SkillDescriptor],
    *,
    pin_for: Any = None,
    search_text_for: Any = None,
) -> tuple[ToolEntry, ...]:
    """``SkillDescriptor`` を ``ToolEntry`` へ正規化する。

    ``pin_for`` / ``search_text_for`` は ``ToolSearchPolicy`` の同名メソッド互換の callable。
    省略時は pin を ``auto``、検索専用語彙を空とする。
    """
    entries: list[ToolEntry] = []
    for skill in skills:
        pin = pin_for(skill.entry_id) if pin_for is not None else "auto"
        search_text = search_text_for(skill.entry_id) if search_text_for is not None else ""
        entries.append(
            ToolEntry(
                id=skill.entry_id,
                kind="skill",
                server=SKILL_SERVER,
                name=skill.tool_name,
                description=skill.description,
                arg_terms=(),
                additional_search_text=search_text,
                pin=pin,
                deferred=pin != "always",
            )
        )
    return tuple(entries)


def read_skill_body(skill: SkillDescriptor, *, max_chars: int = MAX_SKILL_BODY_CHARS) -> str:
    """SKILL.md 本文を返す。上限超過時は切り詰めて続きの読み方を案内する。"""
    try:
        text = skill.path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"skill '{skill.name}' could not be read: {exc}"
    if len(text) <= max_chars:
        return text
    return (
        text[:max_chars]
        + f"\n\n<!-- truncated at {max_chars} chars. Read the rest from: {skill.path.as_posix()} -->\n"
    )


def build_skill_tools(
    skills: Sequence[SkillDescriptor],
    *,
    core_skill_names: Iterable[str] = (),
) -> list[Any]:
    """各 Skill を SDK ツールとして登録する。

    Core Skill は ``defer="never"``（常時公開）、それ以外は ``defer="auto"``（遅延公開）。
    handler は SKILL.md 本文を返す。
    """
    from copilot import define_tool  # 遅延 import（repository_query_tools.py と同じ方針）

    core = set(core_skill_names)
    tools: list[Any] = []
    for skill in skills:
        body_provider = _make_body_provider(skill)
        tools.append(
            define_tool(
                skill.tool_name,
                description=skill.description or f"Skill: {skill.name}",
                handler=body_provider,
                skip_permission=True,
                defer="never" if skill.name in core else "auto",
            )
        )
    return tools


def _make_body_provider(skill: SkillDescriptor):
    async def _handler(_params: Any, _invocation: Any) -> str:
        return read_skill_body(skill)

    return _handler


def skill_manifest_pins(
    manifest: Mapping[str, Any],
    workflow_id: str | None,
    step_id: str | None,
) -> dict[str, str]:
    """``hve/skill_manifest.json`` の必須 Skill を pin 定義へ変換する（FR-TS-03）。

    ``workflow_defaults`` と ``required_skills[workflow][step]`` を ``always`` として扱う。
    ``optional_skills`` は long-tail のため pin にしない。
    """
    if not workflow_id:
        return {}
    pins: dict[str, str] = {}

    defaults = manifest.get("workflow_defaults")
    if isinstance(defaults, Mapping):
        for name in defaults.get(workflow_id, []) or []:
            pins[_skill_entry_id(str(name))] = "always"

    required = manifest.get("required_skills")
    if isinstance(required, Mapping) and step_id:
        per_workflow = required.get(workflow_id)
        if isinstance(per_workflow, Mapping):
            for name in per_workflow.get(step_id, []) or []:
                pins[_skill_entry_id(str(name))] = "always"

    return pins


def _skill_entry_id(skill_name: str) -> str:
    return ToolEntry.make_id("skill", SKILL_SERVER, f"{SKILL_TOOL_PREFIX}{skill_name}")
