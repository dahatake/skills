"""HVE Tool Search — ポリシー解決（FR-TS-03）。

**強制力の所在（重要）**

本モジュールが決めるのは「`tool_search_tool` が **何を返すか**」だけである。
モデルによる呼び出しを禁止する力は持たない。禁止の強制は次の 2 つでしか行えない:

- ``create_session(excluded_tools=[...])``
- MCP サーバー設定の ``tools`` allowlist（``[]`` = なし / ``"*"`` = 全件）

したがって本モジュールを安全境界として扱ってはならない（FR-TS-03）。
``apply_policy()`` の ``excluded_tools`` 引数は「索引から落とす」ためのものであり、
実行時の禁止は呼び出し側が上記 2 つで別途設定する必要がある。
"""

from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .types import PinMode, ToolEntry, ToolSearchContractError, resolve_policy_value
from .usage import LOG_DIRNAME

_POLICY_FILE = Path(__file__).with_name("policy.json")

# キーは常に ToolEntry.id 形式か、サーバーワイルドカード。ツール名だけのキーは拒否する。
POLICY_KEY_RE = re.compile(r"^(mcp|native|skill):[^:*]+:([^:]+|\*)$")

_KEYED_TABLES = ("pins", "additional_search_text")
_VALID_PIN_MODES = ("always", "auto", "never")
_VALID_STEP_MODES = ("search", "pin_only")

_REQUIRED_FIELDS = (
    "version", "limit", "max_limit", "tau",
    "field_weights", "pins", "additional_search_text", "step_overrides",
)
_REQUIRED_WEIGHT_FIELDS = frozenset(
    {"name", "additional_search_text", "description", "arg_terms"}
)


class PolicyError(ToolSearchContractError):
    """policy.json の形式違反。"""


@dataclass(frozen=True)
class PolicyDecision:
    """ポリシー適用の結果。

    ``pinned`` は検索なしで常時公開する集合、``searchable`` は検索の対象。
    ``dropped`` は索引から外した ``ToolEntry.id``（``excluded_tools`` 一致分）。

    ``pin`` の 3 値は「pin するか」だけを表し、公開可否ではない:

    - ``always`` — 常時公開（検索を経ずに呼べる）
    - ``auto``   — 検索対象。利用履歴による自動 pin の**対象になる**（FR-TS-07）
    - ``never``  — 検索対象。自動 pin の**対象にしない**（常に検索経由のまま）

    ``never`` は「索引から消す」ではない。索引から消す唯一の手段は ``excluded_tools``。
    """

    pinned: tuple[ToolEntry, ...] = ()
    searchable: tuple[ToolEntry, ...] = ()
    dropped: tuple[str, ...] = ()

    @property
    def auto_pin_candidates(self) -> tuple[ToolEntry, ...]:
        """自動 pin（FR-TS-07）で昇格させてよいエントリ。"""
        return tuple(entry for entry in self.searchable if entry.pin == "auto")


@dataclass(frozen=True)
class ToolSearchPolicy:
    version: int
    limit: int
    max_limit: int
    tau: float
    field_weights: Mapping[str, float]
    pins: Mapping[str, PinMode]
    additional_search_text: Mapping[str, str]
    step_overrides: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    # --- 読み込みと検証 ---------------------------------------------------
    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ToolSearchPolicy":
        for key in _REQUIRED_FIELDS:
            if key not in raw:
                raise PolicyError(f"policy is missing required field: {key!r}")

        weights = raw["field_weights"]
        if set(weights) != _REQUIRED_WEIGHT_FIELDS:
            raise PolicyError(
                f"field_weights must cover exactly {sorted(_REQUIRED_WEIGHT_FIELDS)}, got {sorted(weights)}"
            )

        for table_name in _KEYED_TABLES:
            for key in raw[table_name]:
                if not POLICY_KEY_RE.match(key):
                    raise PolicyError(
                        f"{table_name} key {key!r} must be '{{kind}}:{{server}}:{{name}}' "
                        "or '{kind}:{server}:*' (bare tool names are rejected because "
                        "names can collide across MCP servers)"
                    )

        for key, mode in raw["pins"].items():
            if mode not in _VALID_PIN_MODES:
                raise PolicyError(f"pins[{key!r}] = {mode!r} is not one of {_VALID_PIN_MODES}")

        for step_key, override in raw["step_overrides"].items():
            mode = override.get("mode")
            if mode not in _VALID_STEP_MODES:
                raise PolicyError(
                    f"step_overrides[{step_key!r}].mode = {mode!r} is not one of {_VALID_STEP_MODES}"
                )

        limit, max_limit = int(raw["limit"]), int(raw["max_limit"])
        if not 1 <= limit <= max_limit:
            raise PolicyError(f"require 1 <= limit <= max_limit, got {limit} and {max_limit}")
        tau = float(raw["tau"])
        if not 0.0 <= tau <= 1.0:
            raise PolicyError(f"tau must be within [0.0, 1.0], got {tau}")

        return cls(
            version=int(raw["version"]),
            limit=limit,
            max_limit=max_limit,
            tau=tau,
            field_weights={k: float(v) for k, v in weights.items()},
            pins=dict(raw["pins"]),
            additional_search_text=dict(raw["additional_search_text"]),
            step_overrides={k: dict(v) for k, v in raw["step_overrides"].items()},
        )

    @staticmethod
    def default_path(repo_root: Path | str | None = None) -> Path:
        """`policy.json` の場所。表示側がパス規則を再実装しないための単一経路。

        ``repo_root`` を渡したとき、そのリポジトリに ``.toolsearch/policy.json`` が
        あればそれを優先する（別リポジトリで使うときの上書き経路）。
        """
        if repo_root is not None:
            local = Path(repo_root) / LOG_DIRNAME / "policy.json"
            if local.is_file():
                return local
        return _POLICY_FILE

    @classmethod
    def load(
        cls,
        path: Path | str | None = None,
        *,
        repo_root: Path | str | None = None,
    ) -> "ToolSearchPolicy":
        target = Path(path) if path is not None else cls.default_path(repo_root)
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except OSError as exc:
            raise PolicyError(f"cannot read policy file: {target}") from exc
        except json.JSONDecodeError as exc:
            raise PolicyError(f"policy file is not valid JSON: {target}") from exc
        return cls.from_dict(raw)

    # --- 参照 -------------------------------------------------------------
    def pin_for(self, entry_id: str) -> PinMode:
        return resolve_policy_value(entry_id, self.pins, "auto")

    def search_text_for(self, entry_id: str) -> str:
        return resolve_policy_value(entry_id, self.additional_search_text, "")

    def effective_limit(self, requested: int | None = None) -> int:
        if requested is None:
            return self.limit
        return max(1, min(int(requested), self.max_limit))

    def mode_for_step(self, workflow_id: str | None, step_id: str | None) -> str:
        """`search` か `pin_only` を返す。step 指定が無ければ `search`。"""
        if not workflow_id or not step_id:
            return "search"
        override = self.step_overrides.get(f"{workflow_id}:{step_id}")
        if not override:
            return "search"
        return str(override.get("mode", "search"))


def apply_policy(
    entries: Sequence[ToolEntry],
    policy: ToolSearchPolicy,
    *,
    excluded_tools: Iterable[str] | None = None,
    pin_only: bool = False,
    manifest_pins: Mapping[str, str] | None = None,
    auto_pins: Iterable[str] | None = None,
) -> PolicyDecision:
    """`ToolEntry` 列へ pin と検索語彙を適用し、pin / 検索対象へ振り分ける。

    優先順位（FR-TS-03、高→低）:
    ``excluded_tools`` > ``manifest_pins`` > ``policy.pins`` > ``auto_pins`` > 検索結果。

    ``auto_pins`` は利用履歴による昇格（FR-TS-07）。``policy.pins`` が ``auto`` のときだけ
    適用する（``always`` / ``never`` の明示指定を上書きしない）。

    `pin_only=True`（fail-closed Step）のときは検索対象を空にする。ただしこれは
    「返さない」だけであり、呼び出しの禁止ではない（モジュール docstring 参照）。
    """
    excluded = {str(name) for name in (excluded_tools or ())}
    overrides = dict(manifest_pins or {})
    promoted = {str(tool_id) for tool_id in (auto_pins or ())}
    pinned: list[ToolEntry] = []
    searchable: list[ToolEntry] = []
    dropped: list[str] = []

    for entry in entries:
        if entry.name in excluded or entry.id in excluded:
            dropped.append(entry.id)
            continue
        pin = overrides.get(entry.id) or policy.pin_for(entry.id)
        if pin == "auto" and entry.id in promoted:
            pin = "always"
        resolved = dataclasses.replace(
            entry,
            additional_search_text=policy.search_text_for(entry.id) or entry.additional_search_text,
            pin=pin,
        )
        if pin == "always":
            pinned.append(resolved)
        else:
            # auto / never のどちらも検索対象。差は自動 pin の対象になるかだけ。
            searchable.append(resolved)

    if pin_only:
        searchable = []

    return PolicyDecision(
        pinned=tuple(pinned),
        searchable=tuple(searchable),
        dropped=tuple(dropped),
    )
