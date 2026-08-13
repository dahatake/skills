"""HVE Tool Search — 契約型（FR-TS-01 / FR-TS-02）。

カタログの唯一の入力は SDK が `tool_search_tool` 呼び出し時にだけ渡す
``ToolInvocation.available_tools``（``CurrentToolMetadata`` の列）である。
HVE 側から MCP へ `tools/list` を発行しない。根拠と実測は
``work/hve-tool-search/contracts/spike-result.md`` を参照。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

# SDK 組み込みのツール検索ツール名（copilot/session.py の _TOOL_SEARCH_TOOL_NAME と一致させる）。
TOOL_SEARCH_TOOL_NAME = "tool_search_tool"

# Skill をツールとして登録するときの名前接頭辞（FR-TS-06）。
# SDK の CurrentToolMetadata には Skill であることを示す属性がないため、名前規約で判別する。
SKILL_TOOL_PREFIX = "skill_"

# Skill エントリの server 値。SDK スナップショットからカテゴリを復元できないため一律とする。
SKILL_SERVER = "skills"

# 引数スキーマを語彙化するときの最大ネスト深さ。Foundry Toolbox と同じ 3 階層。
MAX_ARG_SCHEMA_DEPTH = 3

ToolKind = Literal["mcp", "native", "skill"]
PinMode = Literal["always", "auto", "never"]

_PIN_MODES: frozenset[str] = frozenset(("always", "auto", "never"))


class ToolSearchContractError(ValueError):
    """契約違反（不正な pin 値、id 生成不能など）。"""


def flatten_schema_terms(
    schema: Any,
    *,
    max_depth: int = MAX_ARG_SCHEMA_DEPTH,
) -> tuple[str, ...]:
    """JSON Schema から引数名と引数説明を平坦化して語彙列にする。

    ``max_depth`` を超えるネストは打ち切る（Foundry Toolbox の索引深さに合わせる）。
    出現順を保った重複排除を行う。
    """
    terms: list[str] = []
    seen: set[str] = set()

    def _emit(value: str) -> None:
        text = value.strip()
        if text and text not in seen:
            seen.add(text)
            terms.append(text)

    def _walk(node: Any, depth: int) -> None:
        if depth > max_depth or not isinstance(node, Mapping):
            return
        description = node.get("description")
        if isinstance(description, str):
            _emit(description)
        properties = node.get("properties")
        if isinstance(properties, Mapping):
            for prop_name, prop_schema in properties.items():
                _emit(str(prop_name))
                _walk(prop_schema, depth + 1)
        items = node.get("items")
        if isinstance(items, Mapping):
            _walk(items, depth + 1)

    _walk(schema, 1)
    return tuple(terms)


@dataclass(frozen=True)
class ToolEntry:
    """検索対象として正規化した 1 ツール。

    ``additional_search_text`` は索引にだけ入り、``ToolCard`` へは出さない
    （Foundry Toolbox の `additional_search_text` と同じ契約）。
    """

    id: str
    kind: ToolKind
    server: str
    name: str
    description: str
    arg_terms: tuple[str, ...] = ()
    additional_search_text: str = ""
    pin: PinMode = "auto"
    deferred: bool = True

    def __post_init__(self) -> None:
        if self.pin not in _PIN_MODES:
            raise ToolSearchContractError(
                f"invalid pin mode: {self.pin!r} (expected one of {sorted(_PIN_MODES)})"
            )
        if not self.name:
            raise ToolSearchContractError("ToolEntry.name must not be empty")

    @staticmethod
    def make_id(kind: ToolKind, server: str, name: str) -> str:
        return f"{kind}:{server}:{name}"

    @classmethod
    def from_current_tool_metadata(
        cls,
        metadata: Any,
        *,
        additional_search_text: str = "",
        pin: PinMode = "auto",
    ) -> "ToolEntry":
        """SDK の ``CurrentToolMetadata`` を ``ToolEntry`` へ正規化する。

        属性アクセスのみを使い、SDK のクラスに import 依存しない。
        """
        name = str(getattr(metadata, "name", "") or "")
        if not name:
            raise ToolSearchContractError("CurrentToolMetadata.name is required")
        mcp_server = getattr(metadata, "mcp_server_name", None)
        if mcp_server:
            kind: ToolKind = "mcp"
            server = str(mcp_server)
        elif name.startswith(SKILL_TOOL_PREFIX):
            kind = "skill"
            server = SKILL_SERVER
        else:
            kind = "native"
            server = "hve"
        deferred = bool(getattr(metadata, "defer_loading", False) or False)
        return cls(
            id=cls.make_id(kind, server, name),
            kind=kind,
            server=server,
            name=name,
            description=str(getattr(metadata, "description", "") or ""),
            arg_terms=flatten_schema_terms(getattr(metadata, "input_schema", None)),
            additional_search_text=additional_search_text,
            pin=pin,
            deferred=deferred,
        )


@dataclass(frozen=True)
class ToolCard:
    """``ToolResult.text_result_for_llm`` へ出す人間可読サマリ 1 件分。

    SDK への発見結果の受け渡しは ``ToolResult.tool_references``（名前の列）で行うため、
    本型はモデルが読む説明文の生成にだけ使う。``additional_search_text`` を
    構造上持たないことで FR-TS-02 の「検索専用語彙を返さない」契約を型で保証する。
    """

    name: str
    kind: ToolKind
    server: str
    description: str
    score: float

    @classmethod
    def from_entry(cls, entry: ToolEntry, score: float) -> "ToolCard":
        return cls(
            name=entry.name,
            kind=entry.kind,
            server=entry.server,
            description=entry.description,
            score=score,
        )


def resolve_policy_value(
    entry_id: str,
    table: Mapping[str, Any],
    default: Any = None,
) -> Any:
    """``policy.json`` のキー解決。完全一致 → サーバーワイルドカードの順で引く。

    キーは常に ``ToolEntry.id``（``{kind}:{server}:{name}``）形式とする。
    ツール名だけのキーは **受け付けない**。``name`` は MCP サーバー間で衝突しうるため、
    暗黙のフォールバックは別サーバーの同名ツールへ pin が誤って効く原因になる。
    """
    if entry_id in table:
        return table[entry_id]
    kind, _, rest = entry_id.partition(":")
    server, _, _name = rest.partition(":")
    if server:
        wildcard = f"{kind}:{server}:*"
        if wildcard in table:
            return table[wildcard]
    return default


def build_catalog(
    available_tools: Sequence[Any] | None,
    *,
    additional_search_text: Mapping[str, str] | None = None,
    pins: Mapping[str, PinMode] | None = None,
) -> tuple[ToolEntry, ...]:
    """``ToolInvocation.available_tools`` を ``ToolEntry`` 列へ正規化する。

    ``available_tools`` が ``None``（SDK 側のスナップショット取得失敗）でも例外にせず
    空を返す。呼び出し側は空カタログを「絞り込み不能」として扱う。
    """
    if not available_tools:
        return ()
    extra = additional_search_text or {}
    pin_map = pins or {}
    entries: list[ToolEntry] = []
    seen: set[str] = set()
    for metadata in available_tools:
        entry = ToolEntry.from_current_tool_metadata(metadata)
        if entry.id in seen:
            continue
        seen.add(entry.id)
        entries.append(
            dataclasses.replace(
                entry,
                additional_search_text=resolve_policy_value(entry.id, extra, ""),
                pin=resolve_policy_value(entry.id, pin_map, "auto"),
            )
        )
    return tuple(entries)
