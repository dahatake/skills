"""Shell (Bash) symbol extraction via tree-sitter (FR-CQ-04 / FR-CQ-11)."""

from __future__ import annotations

from cq.languages import ChunkSpan, RawSymbol, treesitter as ts

GRAMMAR = ts.Grammar(
    lang="shell",
    module="tree_sitter_bash",
    kinds={"function_definition": "function"},
    scopes={},
    name_of=lambda n, s: ts.field_name(n, s),
    scope_name_of=lambda n, s: ts.field_name(n, s),
    doc_markers=("#",),
    # `source` / `.` are plain commands in this grammar, so imports cannot be
    # told apart from calls by node type; both land in the reference list.
    call_nodes=frozenset({"command"}),
)


def extract(source: str) -> tuple[RawSymbol, ...]:
    return ts.extract(GRAMMAR, source)


def extract_ex(source: str) -> tuple[tuple[RawSymbol, ...], str]:
    return ts.extract_with_fidelity(GRAMMAR, source)


def chunk_spans(source: str, lines: list[str], max_chars: int) -> tuple[ChunkSpan, ...]:
    return ts.chunk_spans(GRAMMAR, source, lines, max_chars)


def extract_graph(source: str):
    return ts.extract_graph(GRAMMAR, source)
