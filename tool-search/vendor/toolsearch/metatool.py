"""HVE Tool Search — SDK 組み込み `tool_search_tool` の差し替え（FR-TS-01 / FR-TS-08）。

SDK は ``tool_search_tool`` 呼び出し時にだけ ``ToolInvocation.available_tools`` へ
ライブカタログ（MCP + native）を渡す。本モジュールはそれを唯一の入力とし、
HVE 側から MCP へ ``tools/list`` 等の RPC を発行しない。

発見結果は ``ToolResult.tool_references``（ツール名の列）で返し、定義の展開は SDK に委ねる。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, Mapping, Sequence

from pydantic import BaseModel, Field

from .policy import PolicyDecision, ToolSearchPolicy, apply_policy
from .ranking import ToolRanker
from .types import TOOL_SEARCH_TOOL_NAME, ToolCard, ToolEntry, build_catalog

TOOL_SEARCH_DESCRIPTION = (
    "Find tools by capability. Describe in natural language what you need to do "
    "(Japanese or English). Call this before concluding that a capability is unavailable. "
    "Returns a shortlist of matching tools that become callable for the rest of the turn."
)

# FR-TS-08: 遅延公開が発火していないときのメッセージ。
NO_DEFERRED_TOOLS_WARNING = (
    "no deferred tools were present in the live catalog: the SDK's tool_search "
    "defer threshold may not have been reached, so this ranker has no effect"
)

EMPTY_CATALOG_MESSAGE = "the live tool catalog snapshot was unavailable; no tools could be ranked"


class ToolSearchParams(BaseModel):
    """差し替え後の `tool_search_tool` が受け取る引数。"""

    query: str = Field(description="Natural-language description of the capability you need.")
    limit: int | None = Field(
        default=None,
        description="Maximum number of tools to return (default 5, max 10).",
    )


@dataclass(frozen=True)
class SearchOutcome:
    references: tuple[str, ...] = ()
    summary: str = ""
    warnings: tuple[str, ...] = ()


@dataclass
class ToolSearchContext:
    """1 セッション分の検索設定。

    ``on_event`` は ``(event_name, payload)`` を受ける任意のコールバック。
    ``run_journal`` への記録は呼び出し側で束ねる（本モジュールは journal へ直接依存しない）。
    """

    policy: ToolSearchPolicy
    skill_entries: tuple[ToolEntry, ...] = ()
    manifest_pins: Mapping[str, str] = field(default_factory=dict)
    auto_pins: tuple[str, ...] = ()
    excluded_tools: tuple[str, ...] = ()
    workflow_id: str | None = None
    step_id: str | None = None
    on_event: Callable[[str, Mapping[str, Any]], None] | None = None

    # モデル向けツール名 → ToolEntry.id。セッション内で名前は一意なのでこの対応は一意。
    # 利用履歴（FR-TS-07）を id で記録するために使う。
    name_to_id: dict[str, str] = field(default_factory=dict)

    # FR-TS-09: 直近に `toolsearch.catalog` を出したカタログの署名。
    # 同じカタログで検索が繰り返されても構成イベントは 1 回だけ出す。
    _catalog_signature: str = field(default="", repr=False)

    def emit(self, event: str, payload: Mapping[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(event, payload)
        except Exception:  # 記録の失敗で検索を止めない
            pass


def decide_catalog(
    context: ToolSearchContext,
    available_tools: Sequence[Any] | None,
) -> tuple[PolicyDecision, tuple[str, ...]]:
    """ライブカタログと Skill カタログを合流させ、ポリシーを適用する。"""
    warnings: list[str] = []
    live = build_catalog(available_tools)
    if not live and not context.skill_entries:
        warnings.append(EMPTY_CATALOG_MESSAGE)
    elif live and not any(entry.deferred for entry in live):
        warnings.append(NO_DEFERRED_TOOLS_WARNING)

    merged = list(live) + list(context.skill_entries)
    context.name_to_id.update({entry.name: entry.id for entry in merged})
    pin_only = context.policy.mode_for_step(context.workflow_id, context.step_id) == "pin_only"
    decision = apply_policy(
        merged,
        context.policy,
        excluded_tools=context.excluded_tools,
        pin_only=pin_only,
        manifest_pins=context.manifest_pins,
        auto_pins=context.auto_pins,
    )
    _emit_catalog_shape(context, merged, decision)
    return decision, tuple(warnings)


@lru_cache(maxsize=4096)
def _entry_tokens(entry: ToolEntry) -> int:
    """モデルへ渡る定義相当のトークン量。エントリは frozen なのでキャッシュできる。"""
    from .eval import entry_definition_text, estimate_tokens

    return estimate_tokens(entry_definition_text(entry))


def catalog_shape(entries: Sequence[ToolEntry], decision: PolicyDecision) -> dict[str, int]:
    """FR-TS-09: ダッシュボードが読むカタログ構成。"""
    kinds = {"mcp": 0, "native": 0, "skill": 0}
    for entry in entries:
        if entry.kind in kinds:
            kinds[entry.kind] += 1
    return {
        "total": len(entries),
        "pinned": len(decision.pinned),
        "searchable": len(decision.searchable),
        "dropped": len(decision.dropped),
        "deferred": sum(1 for entry in entries if entry.deferred),
        **kinds,
    }


def _emit_catalog_shape(
    context: ToolSearchContext,
    entries: Sequence[ToolEntry],
    decision: PolicyDecision,
) -> None:
    """カタログの顔ぶれが変わったときだけ構成イベントを出す。"""
    signature = "|".join(entry.id for entry in entries)
    if signature == context._catalog_signature:
        return
    context._catalog_signature = signature
    context.emit(
        "toolsearch.catalog",
        {
            "catalog": catalog_shape(entries, decision),
            "entry_ids": [entry.id for entry in entries],
            "names": {entry.id: entry.name for entry in entries},
            "pinned_ids": [entry.id for entry in decision.pinned],
        },
    )


def search_catalog(
    context: ToolSearchContext,
    available_tools: Sequence[Any] | None,
    query: str,
    limit: int | None = None,
) -> SearchOutcome:
    started = time.perf_counter()
    decision, warnings = decide_catalog(context, available_tools)
    effective_limit = context.policy.effective_limit(limit)

    ranker = ToolRanker(
        decision.searchable,
        context.policy.field_weights,
        tau=context.policy.tau,
    )
    ranked = ranker.search(query, limit=effective_limit)
    cards = tuple(ToolCard.from_entry(item.entry, item.score) for item in ranked)
    latency_ms = (time.perf_counter() - started) * 1000.0

    all_entries = tuple(decision.pinned) + tuple(decision.searchable)
    exposed = tuple(decision.pinned) + tuple(item.entry for item in ranked)
    context.emit(
        "toolsearch.query",
        {
            "query": query,
            "limit": effective_limit,
            "searchable": len(decision.searchable),
            "pinned": len(decision.pinned),
            "hits": [card.name for card in cards],
            "scores": [round(card.score, 6) for card in cards],
            "latency_ms": round(latency_ms, 3),
            "engine": ranker.engine_name,
            "catalog": catalog_shape(all_entries, decision),
            "tokens": {
                "baseline": sum(_entry_tokens(entry) for entry in all_entries),
                "exposed": sum(_entry_tokens(entry) for entry in exposed),
            },
            "warnings": list(warnings),
        },
    )
    if not cards:
        context.emit("toolsearch.miss", {"query": query, "searchable": len(decision.searchable)})

    return SearchOutcome(
        references=tuple(card.name for card in cards),
        summary=render_summary(cards, warnings=warnings, query=query),
        warnings=warnings,
    )


def render_summary(
    cards: Sequence[ToolCard],
    *,
    warnings: Sequence[str] = (),
    query: str = "",
) -> str:
    """モデルへ返す人間可読サマリ。検索専用語彙は含めない（`ToolCard` が持たない）。"""
    lines: list[str] = []
    for warning in warnings:
        lines.append(f"warning: {warning}")
    if not cards:
        lines.append(
            f"No tool matched {query!r}. Try different wording, or state that the capability is unavailable."
        )
    else:
        lines.append(f"{len(cards)} tool(s) matched and are now callable:")
        lines.extend(f"- {card.name} ({card.server}): {card.description}" for card in cards)
    return "\n".join(lines)


def build_tool_search_tool(context: ToolSearchContext) -> Any:
    """SDK 組み込み `tool_search_tool` を差し替えるツールを返す。"""
    from copilot import define_tool
    from copilot.tools import ToolResult

    async def _handler(params: ToolSearchParams, invocation: Any) -> Any:
        outcome = search_catalog(
            context,
            getattr(invocation, "available_tools", None),
            params.query,
            params.limit,
        )
        return ToolResult(
            text_result_for_llm=outcome.summary,
            tool_references=list(outcome.references),
        )

    return define_tool(
        TOOL_SEARCH_TOOL_NAME,
        description=TOOL_SEARCH_DESCRIPTION,
        handler=_handler,
        params_type=ToolSearchParams,
        overrides_built_in_tool=True,
        skip_permission=True,
    )
