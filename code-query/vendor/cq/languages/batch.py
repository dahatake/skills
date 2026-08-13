"""Windows batch symbol extraction via tree-sitter (FR-CQ-04 / FR-CQ-11).

この文法に関数の概念は無い。`call :name` の飛び先である `label` が唯一の
定義単位なので、それを `function` として索引する。
"""

from __future__ import annotations

import re

from cq.languages import ChunkSpan, RawSymbol, treesitter as ts

# `call :label args` の飛び先。`call other.cmd` は command_name を持つので通らない。
_CALL_LABEL = re.compile(r"^\s*call\s+:([^\s%]+)", re.IGNORECASE)


def _name(node, src) -> str:
    return ts.text(node, src).lstrip(":").strip()


def _callee(node, src) -> str:
    target = ts.named_child_by_type(node, "command_name")
    if target is not None:
        return ts.text(target, src)
    match = _CALL_LABEL.match(ts.text(node, src))
    return match.group(1) if match else ""


GRAMMAR = ts.Grammar(
    lang="batch",
    module="tree_sitter_batch",
    kinds={"label": "function"},
    scopes={},
    name_of=_name,
    scope_name_of=_name,
    doc_markers=("::", "REM", "rem"),
    call_nodes=frozenset({"cmd", "call_stmt"}),
    callee_of=_callee,
)


def extract(source: str) -> tuple[RawSymbol, ...]:
    return ts.extract(GRAMMAR, source)


def extract_ex(source: str) -> tuple[tuple[RawSymbol, ...], str]:
    return ts.extract_with_fidelity(GRAMMAR, source)


def chunk_spans(source: str, lines: list[str], max_chars: int) -> tuple[ChunkSpan, ...]:
    return ts.chunk_spans(GRAMMAR, source, lines, max_chars)


def extract_graph(source: str):
    return ts.extract_graph(GRAMMAR, source)
