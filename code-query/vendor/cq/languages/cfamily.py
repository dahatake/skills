"""C and C++ symbol extraction via tree-sitter (FR-CQ-04 / FR-CQ-11).

Both languages share the declarator plumbing, and `.h` is resolved by content
because the suffix alone cannot tell them apart.
"""

from __future__ import annotations

from cq.languages import ChunkSpan, RawSymbol, treesitter as ts

_DECLARATOR_THROUGH = frozenset({
    "init_declarator",
    "pointer_declarator",
    "reference_declarator",
    "array_declarator",
    "function_declarator",
    "parenthesized_declarator",
    "attributed_declarator",
})
_DECLARATOR_NAMES = frozenset({
    "identifier",
    "field_identifier",
    "type_identifier",
    "qualified_identifier",
    "destructor_name",
    "operator_name",
})

# Node types tree-sitter-cpp produces that tree-sitter-c cannot. Parse-error
# counting alone cannot separate the two, because a plain C header is also valid
# C++ (measured: both grammars report zero errors for such a header).
_CPP_ONLY_NODES = frozenset({
    "namespace_definition",
    "class_specifier",
    "template_declaration",
    "qualified_identifier",
    "access_specifier",
    "reference_declarator",
    "using_declaration",
    "alias_declaration",
    "destructor_name",
    "operator_name",
})

# `preproc_function_def` exposes `name` / `parameters`, never `declarator`.
_NAMED_BY_FIELD = frozenset({
    "struct_specifier",
    "union_specifier",
    "enum_specifier",
    "class_specifier",
    "namespace_definition",
    "alias_declaration",
    "preproc_function_def",
})


def _name(node, src) -> str:
    if node.type in _NAMED_BY_FIELD:
        # An anonymous namespace yields "", which suppresses both the symbol and
        # the scope component so qualnames stay searchable.
        return ts.field_name(node, src)
    declarator = node.child_by_field_name("declarator")
    found = ts.descend_for(declarator, _DECLARATOR_NAMES, _DECLARATOR_THROUGH)
    return ts.text(found, src) if found is not None else ""


def _is_function_shaped(node) -> bool:
    declarator = node.child_by_field_name("declarator")
    return ts.descend_for(
        declarator, frozenset({"function_declarator"}), _DECLARATOR_THROUGH
    ) is not None


def _declaration_name(node, src) -> str:
    """Variables are out of the contract's initial scope; only functions count."""
    if node.type in ("declaration", "field_declaration") and not _is_function_shaped(node):
        return ""
    return _name(node, src)


def _cpp_resolve(node, src, kind, name, scope, local_types):
    kind, local, qual_scope, bound = ts.default_resolve(
        node, src, kind, name, scope, local_types
    )
    if "::" in name:
        # Out-of-class definition (`bool svc::Ledger::helper(int)`): the qualifier
        # tail may be a class or a namespace, so resolve it against this file.
        head, _, local = name.rpartition("::")
        qual_scope = tuple(head.split("::"))
        bound = qual_scope[-1] in local_types
    return kind, local, qual_scope, bound


def _include(node, src) -> str:
    path = node.child_by_field_name("path")
    return ts.text(path, src).strip('"<>') if path is not None else ""


_C_KINDS = {
    "struct_specifier": "struct",
    "union_specifier": "struct",
    "enum_specifier": "enum",
    "type_definition": "type",
    "function_definition": "function",
    "declaration": "prototype",
    "preproc_function_def": "macro",
}

C_GRAMMAR = ts.Grammar(
    lang="c",
    module="tree_sitter_c",
    kinds=_C_KINDS,
    scopes={},
    name_of=_declaration_name,
    scope_name_of=_name,
    doc_markers=("/**", "//"),
    call_nodes=frozenset({"call_expression"}),
    import_nodes=frozenset({"preproc_include"}),
    import_of=_include,
)

CPP_GRAMMAR = ts.Grammar(
    lang="cpp",
    module="tree_sitter_cpp",
    kinds={
        **_C_KINDS,
        "class_specifier": "class",
        "namespace_definition": "namespace",
        "alias_declaration": "type",
        "field_declaration": "prototype",
    },
    scopes={
        "class_specifier": "class",
        "struct_specifier": "struct",
        "enum_specifier": "enum",
        "namespace_definition": "namespace",
    },
    name_of=_declaration_name,
    scope_name_of=_name,
    doc_markers=("/**", "//"),
    resolve=_cpp_resolve,
    sep="::",
    call_nodes=frozenset({"call_expression"}),
    import_nodes=frozenset({"preproc_include"}),
    import_of=_include,
)


def _node_types(node, acc: set[str]) -> None:
    acc.add(node.type)
    for child in node.named_children:
        _node_types(child, acc)


def _errors(node) -> int:
    total = 1 if node.type == "ERROR" or node.is_missing else 0
    for child in node.children:
        total += _errors(child)
    return total


def language_for_header(source: str) -> str:
    """Decide whether a `.h` is C or C++ from its parse tree."""
    try:
        cpp_root = ts.parse(CPP_GRAMMAR, source).root_node
        c_root = ts.parse(C_GRAMMAR, source).root_node
    except Exception:
        return "c"  # 文法未導入なら regex 降格で扱える C を選ぶ
    types: set[str] = set()
    _node_types(cpp_root, types)
    if types & _CPP_ONLY_NODES:
        return "cpp"
    return "c" if _errors(c_root) <= _errors(cpp_root) else "cpp"


def extract_c(source: str) -> tuple[RawSymbol, ...]:
    return ts.extract(C_GRAMMAR, source)


def extract_ex_c(source: str) -> tuple[tuple[RawSymbol, ...], str]:
    return ts.extract_with_fidelity(C_GRAMMAR, source)


def chunk_spans_c(source: str, lines: list[str], max_chars: int) -> tuple[ChunkSpan, ...]:
    return ts.chunk_spans(C_GRAMMAR, source, lines, max_chars)


def extract_graph_c(source: str):
    return ts.extract_graph(C_GRAMMAR, source)


def extract_cpp(source: str) -> tuple[RawSymbol, ...]:
    return ts.extract(CPP_GRAMMAR, source)


def extract_ex_cpp(source: str) -> tuple[tuple[RawSymbol, ...], str]:
    return ts.extract_with_fidelity(CPP_GRAMMAR, source)


def chunk_spans_cpp(source: str, lines: list[str], max_chars: int) -> tuple[ChunkSpan, ...]:
    return ts.chunk_spans(CPP_GRAMMAR, source, lines, max_chars)


def extract_graph_cpp(source: str):
    return ts.extract_graph(CPP_GRAMMAR, source)
