"""HVE Tool Search — Step 実行セッションのコンテキスト内訳の実測（FR-TS-11）。

`session.metadata.contextInfo`（`toolDefinitionsTokens` は "excludes deferred tools"）と
`session.metadata.getContextAttribution` を唯一の情報源とし、推定トークンでは代替しない。
プロンプトは送らないためモデル推論は発生しない。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

BUILTIN_LAYER = "(builtin)"
_TOOL_DEFINITION_PREFIX = "toolDefinition:"
_SYSTEM_PROMPT_ID = "system:systemPrompt"

# stdio MCP は起動に時間がかかる（実測: `azure` は 3.7〜5.1 秒）。
CONNECT_TIMEOUT_SECONDS = 60.0
_POLL_INTERVAL_SECONDS = 0.5


class ContextReportError(RuntimeError):
    """実測に失敗した。推定値で埋めずに呼び出し側へ返す。"""


@dataclass(frozen=True)
class Layer:
    name: str
    tool_count: int
    tokens: int


@dataclass(frozen=True)
class ContextReport:
    model_name: str
    limit: int
    total_tokens: int
    system_tokens: int
    tool_definitions_tokens: int
    mcp_tools_tokens: int
    conversation_tokens: int
    system_prompt_tokens: int | None
    layers: tuple[Layer, ...]
    unconnected: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "limit": self.limit,
            "total_tokens": self.total_tokens,
            "system_tokens": self.system_tokens,
            "tool_definitions_tokens": self.tool_definitions_tokens,
            "mcp_tools_tokens": self.mcp_tools_tokens,
            "conversation_tokens": self.conversation_tokens,
            "system_prompt_tokens": self.system_prompt_tokens,
            "layers": [
                {"name": layer.name, "tool_count": layer.tool_count, "tokens": layer.tokens}
                for layer in self.layers
            ],
            "unconnected": list(self.unconnected),
        }


def build_report(
    *,
    context_info: Mapping[str, Any],
    entries: Sequence[Mapping[str, Any]],
    tools: Sequence[Any],
    connected: Iterable[str],
    declared: Iterable[str],
) -> ContextReport:
    """ランタイムのスナップショットを層別レポートへ正規化する。"""
    server_of = {
        str(getattr(tool, "name", "")): (getattr(tool, "mcp_server_name", None) or BUILTIN_LAYER)
        for tool in tools
    }
    counts: dict[str, int] = {}
    tokens: dict[str, int] = {}
    system_prompt_tokens: int | None = None

    for entry in entries:
        entry_id = str(entry.get("id", ""))
        if entry_id == _SYSTEM_PROMPT_ID:
            system_prompt_tokens = int(entry.get("tokens", 0))
            continue
        if entry.get("kind") != "toolDefinition":
            continue
        name = entry_id[len(_TOOL_DEFINITION_PREFIX):] if entry_id.startswith(
            _TOOL_DEFINITION_PREFIX
        ) else str(entry.get("label", ""))
        # MCP ツールは getCurrentMetadata に必ず現れる。現れないのは組み込み側だけ
        # （実測: `web_search` は attribution にのみ出る）。捨てると合計が合わなくなる。
        server = server_of.get(name, BUILTIN_LAYER)
        counts[server] = counts.get(server, 0) + 1
        tokens[server] = tokens.get(server, 0) + int(entry.get("tokens", 0))

    layers = tuple(
        Layer(name=server, tool_count=counts[server], tokens=tokens[server])
        for server in sorted(counts, key=lambda s: (-tokens[s], s))
    )
    connected_set = {str(name) for name in connected}
    unconnected = tuple(
        name for name in sorted({str(n) for n in declared}) if name not in connected_set
    )
    return ContextReport(
        model_name=str(context_info.get("modelName", "")),
        limit=int(context_info.get("limit", 0)),
        total_tokens=int(context_info.get("totalTokens", 0)),
        system_tokens=int(context_info.get("systemTokens", 0)),
        tool_definitions_tokens=int(context_info.get("toolDefinitionsTokens", 0)),
        mcp_tools_tokens=int(context_info.get("mcpToolsTokens", 0)),
        conversation_tokens=int(context_info.get("conversationTokens", 0)),
        system_prompt_tokens=system_prompt_tokens,
        layers=layers,
        unconnected=unconnected,
    )


def _layer_label(name: str) -> str:
    return "組み込みツール定義 (builtin)" if name == BUILTIN_LAYER else name


def render_text(report: ContextReport) -> str:
    lines = [
        "Step 実行セッションのコンテキスト内訳（実測）",
        f"  モデル (トークナイザ): {report.model_name}",
        f"  コンテキスト上限      : {report.limit:,} tokens",
        "",
        f"  合計                  : {report.total_tokens:,} tokens",
        f"  システムプロンプト等  : {report.system_tokens:,} tokens",
        f"  ツール定義            : {report.tool_definitions_tokens:,} tokens",
        f"  うち MCP              : {report.mcp_tools_tokens:,} tokens",
        f"  会話                  : {report.conversation_tokens:,} tokens",
        "",
        "  ツール定義の層別内訳",
        f"  {'層':<28}{'ツール数':>8}{'tokens':>12}",
    ]
    for layer in report.layers:
        lines.append(f"  {_layer_label(layer.name):<28}{layer.tool_count:>8}{layer.tokens:>12,}")
    if not report.layers:
        lines.append("  (データ不足)")
    lines += ["", "  未接続の宣言済み MCP サーバー"]
    lines.append("  " + (", ".join(report.unconnected) if report.unconnected else "なし"))
    return "\n".join(lines)


def render_json(report: ContextReport) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=2)


async def _connected_servers(session: Any) -> set[str]:
    listing = await session.rpc.mcp.list(timeout=CONNECT_TIMEOUT_SECONDS)
    connected: set[str] = set()
    for server in getattr(listing, "servers", []) or []:
        status = getattr(server, "status", None)
        if str(getattr(status, "value", status) or "").casefold() == "connected":
            connected.add(str(getattr(server, "name", "")))
    return connected


async def _wait_for_servers(session: Any, expected: set[str]) -> set[str]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CONNECT_TIMEOUT_SECONDS
    connected = await _connected_servers(session)
    while expected - connected and loop.time() < deadline:
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        connected = await _connected_servers(session)
    return connected


async def collect(*, repo_root: Path | str) -> ContextReport:
    """Step 実行と同じ経路でセッションを張り、コンテキスト内訳を実測する。

    プロンプトは一切送らない。失敗は ``ContextReportError`` として返す。
    """
    from copilot.generated.rpc import MetadataContextInfoRequest

    from ..config import SDKConfig
    from ..copilot_client_factory import create_copilot_client
    from ..runner import _create_session_with_auto_reasoning_fallback, _read_repository_mcp_config

    root = Path(repo_root)
    declared = set(_read_repository_mcp_config(root))
    config = SDKConfig.from_env()
    try:
        client = create_copilot_client(
            cli_path=config.cli_path,
            cli_url=config.cli_url,
            github_token=config.resolve_token() or None,
            log_level="error",
            cli_args=config.cli_args,
        )
        await client.start()
    except Exception as exc:
        raise ContextReportError(f"Copilot CLI を起動できません: {type(exc).__name__}: {exc}") from exc

    try:
        session = await _create_session_with_auto_reasoning_fallback(client, {"streaming": True})
        try:
            connected = await _wait_for_servers(session, declared)
            await session.rpc.tools.initialize_and_validate(timeout=CONNECT_TIMEOUT_SECONDS)
            info = await session.rpc.metadata.context_info(
                MetadataContextInfoRequest(output_token_limit=0, prompt_token_limit=0),
                timeout=CONNECT_TIMEOUT_SECONDS,
            )
            attribution = await session.rpc.metadata.get_context_attribution(
                timeout=CONNECT_TIMEOUT_SECONDS
            )
            metadata = await session.rpc.tools.get_current_metadata(
                timeout=CONNECT_TIMEOUT_SECONDS
            )
        finally:
            try:
                await session.close()
            except Exception:
                pass
        if info.context_info is None:
            raise ContextReportError("ランタイムがコンテキスト内訳を返しませんでした。")
        entries = [
            entry.to_dict()
            for entry in (
                attribution.context_attribution.entries
                if attribution.context_attribution is not None
                else []
            )
        ]
        return build_report(
            context_info=info.context_info.to_dict(),
            entries=entries,
            tools=list(metadata.tools or []),
            connected=connected,
            declared=declared,
        )
    except ContextReportError:
        raise
    except Exception as exc:
        raise ContextReportError(f"実測に失敗しました: {type(exc).__name__}: {exc}") from exc
    finally:
        try:
            await client.stop()
        except Exception:
            pass


__all__ = [
    "BUILTIN_LAYER",
    "ContextReport",
    "ContextReportError",
    "Layer",
    "build_report",
    "collect",
    "render_json",
    "render_text",
]
