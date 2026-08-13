"""Java symbol extraction via tree-sitter (FR-CQ-04 / FR-CQ-11)."""

from __future__ import annotations

from cq.languages import ChunkSpan, RawSymbol, treesitter as ts

_TYPES = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "class",
    "annotation_type_declaration": "interface",
}

_TEST_ANNOTATIONS = frozenset({"Test", "ParameterizedTest", "RepeatedTest"})


def _annotations(node, src) -> tuple[str, ...]:
    modifiers = ts.named_child_by_type(node, "modifiers")
    if modifiers is None:
        return ()
    names = []
    for child in modifiers.named_children:
        if child.type in ("marker_annotation", "annotation"):
            name = child.child_by_field_name("name")
            if name is not None:
                names.append(ts.text(name, src))
    return tuple(names)


def _is_test(node, src, _name: str) -> bool:
    return any(a in _TEST_ANNOTATIONS for a in _annotations(node, src))


def _import(node, src) -> str:
    target = ts.named_child_by_type(node, "scoped_identifier", "identifier")
    return ts.text(target, src) if target is not None else ""


GRAMMAR = ts.Grammar(
    lang="java",
    module="tree_sitter_java",
    kinds={
        **_TYPES,
        "method_declaration": "method",
        "constructor_declaration": "constructor",
        "compact_constructor_declaration": "constructor",
    },
    scopes=_TYPES,
    # An anonymous class body hangs off object_creation_expression; skipping the
    # subtree keeps qualnames honest instead of re-parenting its methods to the
    # enclosing named class.
    skip=frozenset({"object_creation_expression"}),
    name_of=lambda n, s: ts.field_name(n, s),
    scope_name_of=lambda n, s: ts.field_name(n, s),
    doc_markers=("/**",),
    decorators_of=_annotations,
    is_test_of=_is_test,
    call_nodes=frozenset({"method_invocation"}),
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
