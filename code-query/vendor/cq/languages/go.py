"""Go symbol extraction via tree-sitter (FR-CQ-04 / FR-CQ-11)."""

from __future__ import annotations

from cq.languages import ChunkSpan, RawSymbol, treesitter as ts

_TEST_PREFIXES = ("Test", "Benchmark", "Fuzz", "Example")


def _type_spec_kind(node, src) -> str:
    body = node.child_by_field_name("type")
    if body is None:
        return "type"
    if body.type == "struct_type":
        return "struct"
    if body.type == "interface_type":
        return "interface"
    return "type"


def _receiver_type(node, src) -> str:
    receiver = node.child_by_field_name("receiver")
    if receiver is None:
        return ""
    declaration = ts.named_child_by_type(receiver, "parameter_declaration")
    if declaration is None:
        return ""
    target = declaration.child_by_field_name("type")
    # Unwrap *T and T[U] through the tree so pointers and generics both resolve.
    while target is not None and target.type in ("pointer_type", "generic_type"):
        inner = ts.named_child_by_type(target, "type_identifier", "pointer_type", "generic_type")
        if inner is None:
            break
        target = inner
    return ts.text(target, src) if target is not None else ""


def _resolve(node, src, kind, name, scope, local_types):
    kind, local, qual_scope, bound = ts.default_resolve(
        node, src, kind, name, scope, local_types
    )
    if node.type == "type_spec":
        kind = _type_spec_kind(node, src)
    elif node.type == "method_declaration":
        receiver = _receiver_type(node, src)
        if receiver:
            qual_scope, bound = (receiver,), True
    return kind, local, qual_scope, bound


def _is_test(node, src, name: str) -> bool:
    return name.startswith(_TEST_PREFIXES)


def _import(node, src) -> str:
    path = node.child_by_field_name("path")
    return ts.text(path, src).strip('"`') if path is not None else ""


GRAMMAR = ts.Grammar(
    lang="go",
    module="tree_sitter_go",
    kinds={
        "type_spec": "type",  # refined by _resolve
        "type_alias": "type",
        "function_declaration": "function",
        "method_declaration": "method",
        "method_elem": "method",
        "method_spec": "method",
    },
    scopes={"type_spec": "type"},
    name_of=lambda n, s: ts.field_name(n, s),
    scope_name_of=lambda n, s: ts.field_name(n, s),
    doc_markers=("//",),
    is_test_of=_is_test,
    resolve=_resolve,
    call_nodes=frozenset({"call_expression"}),
    import_nodes=frozenset({"import_spec"}),
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
