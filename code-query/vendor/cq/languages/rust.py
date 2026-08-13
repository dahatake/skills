"""Rust symbol extraction via tree-sitter (FR-CQ-04 / FR-CQ-11)."""

from __future__ import annotations

from cq.languages import ChunkSpan, RawSymbol, treesitter as ts

_TYPES = {
    "struct_item": "struct",
    "union_item": "struct",
    "enum_item": "enum",
    "trait_item": "interface",
    "type_item": "type",
    "mod_item": "module",
}

_TEST_ATTRIBUTES = frozenset({"test", "cfg(test)", "tokio::test"})


def _attributes(node, src) -> tuple[str, ...]:
    found = []
    previous = node.prev_sibling
    while previous is not None and previous.type == "attribute_item":
        found.append(" ".join(ts.text(previous, src).split()))
        previous = previous.prev_sibling
    return tuple(reversed(found))


def _is_test(node, src, _name: str) -> bool:
    # Compare the attribute itself, not a substring: `#[cfg(feature = "latest")]`
    # contains "test" but is not a test marker.
    return any(a.strip("#[] ").replace(" ", "") in _TEST_ATTRIBUTES for a in _attributes(node, src))


def _impl_name(node, src) -> str:
    target = node.child_by_field_name("type")
    if target is None:
        return ""
    if target.type == "generic_type":
        inner = target.child_by_field_name("type")
        if inner is not None:
            target = inner
    return ts.text(target, src)


def _scope_name(node, src) -> str:
    return _impl_name(node, src) if node.type == "impl_item" else ts.field_name(node, src)


def _import(node, src) -> str:
    argument = node.child_by_field_name("argument")
    return " ".join(ts.text(argument, src).split()) if argument is not None else ""


GRAMMAR = ts.Grammar(
    lang="rust",
    module="tree_sitter_rust",
    kinds={
        **_TYPES,
        "function_item": "function",
        "function_signature_item": "function",
        "macro_definition": "macro",
    },
    scopes={**_TYPES, "impl_item": "impl"},
    name_of=lambda n, s: ts.field_name(n, s),
    scope_name_of=_scope_name,
    doc_markers=("///", "//!", "//"),
    decorators_of=_attributes,
    is_test_of=_is_test,
    call_nodes=frozenset({"call_expression"}),
    import_nodes=frozenset({"use_declaration"}),
    import_of=_import,
)


def extract(source: str) -> tuple[RawSymbol, ...]:
    return ts.extract(GRAMMAR, source)


def extract_ex(source: str) -> tuple[tuple[RawSymbol, ...], str]:
    return ts.extract_with_fidelity(GRAMMAR, source)


def chunk_spans(source: str, lines: list[str], max_chars: int) -> tuple[ChunkSpan, ...]:
    return ts.chunk_spans(GRAMMAR, source, lines, max_chars)


def extract_graph(source: str):
    return ts.extract_graph(GRAMMAR, source)
