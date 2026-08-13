"""Scala symbol extraction via tree-sitter (FR-CQ-04 / FR-CQ-11)."""

from __future__ import annotations

from cq.languages import ChunkSpan, RawSymbol, treesitter as ts

# `object` is a singleton whose `def`s are reached as `Name.member`, so it binds
# its members like a class does rather than acting as a Rust-style module.
_TYPES = {
    "object_definition": "class",
    "class_definition": "class",
    "trait_definition": "interface",
    "enum_definition": "enum",
}

# `val_definition` / `var_definition` / `given_definition` also match a local
# declaration inside a `def`'s body (tree-sitter uses the same node type for
# both), which would flood the index with locals unrelated to the file's
# public surface. Only a direct class/trait/object member — whose parent is
# the type's `template_body` — is indexed (FR-CQ-11 Phase 3).
_MEMBER_VALUE_FIELD = {
    "val_definition": "pattern",
    "var_definition": "pattern",
    "given_definition": "name",
}


def _import(node, src) -> str:
    """`import a.b.{C, D}` keeps its selector clause as the canonical specifier."""
    body = ts.text(node, src).split(maxsplit=1)
    return " ".join(body[1].split()) if len(body) == 2 else ""


def _name(node, src) -> str:
    # `type Alias = Other` carries no name field; the first type_identifier is it.
    if node.type == "type_definition":
        target = ts.named_child_by_type(node, "type_identifier")
        return ts.text(target, src) if target is not None else ""
    field = _MEMBER_VALUE_FIELD.get(node.type)
    if field is not None:
        if node.parent is None or node.parent.type != "template_body":
            return ""
        return ts.field_name(node, src, field)
    return ts.field_name(node, src)


GRAMMAR = ts.Grammar(
    lang="scala",
    module="tree_sitter_scala",
    kinds={
        **_TYPES,
        "type_definition": "type",
        "function_definition": "function",
        "function_declaration": "function",
        "val_definition": "variable",
        "var_definition": "variable",
        "given_definition": "variable",
        "class_parameter": "property",
    },
    scopes=_TYPES,
    name_of=_name,
    scope_name_of=lambda n, s: ts.field_name(n, s),
    doc_markers=("/**", "//"),
    call_nodes=frozenset({"call_expression"}),
    import_nodes=frozenset({"import_declaration"}),
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
