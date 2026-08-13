"""TypeScript symbol extraction (FR-CQ-04 / FR-CQ-11).

The primary extractor is the official `tree-sitter-typescript` grammar
(optional dependency), which also backs `.tsx` (registered as the separate
language `"tsx"`, since the package exposes `language_typescript()` and
`language_tsx()` as two distinct factories rather than one `language()`).
A brace-aware regex extractor is kept as the fallback; it shares JavaScript's
scanner (`javascript.scan`) and adds the declarations that exist only in
TypeScript, plus method signatures carrying a return-type annotation.
"""

from __future__ import annotations

import re

from cq.languages import ChunkSpan, ExtractionError, RawSymbol, treesitter as ts
from cq.languages import javascript

# --------------------------------------------------------------------------
# tree-sitter tier (primary)
# --------------------------------------------------------------------------

_TS_ONLY_TYPES = {
    "interface_declaration": "interface",
    "abstract_class_declaration": "class",
    "enum_declaration": "enum",
}

_TS_KINDS = {
    **javascript.GRAMMAR.kinds,
    **_TS_ONLY_TYPES,
    "type_alias_declaration": "type",
    "method_signature": "method",  # interface method declaration (no body)
}

_TS_SCOPES = {
    **javascript.GRAMMAR.scopes,
    "interface_declaration": "interface",
    "abstract_class_declaration": "class",
}


def _make_grammar(lang: str, language_func: str) -> ts.Grammar:
    return ts.Grammar(
        lang=lang,
        module="tree_sitter_typescript",
        language_func=language_func,
        kinds=_TS_KINDS,
        scopes=_TS_SCOPES,
        name_of=javascript._name_of,
        scope_name_of=lambda n, s: ts.field_name(n, s),
        doc_markers=("/**",),
        is_test_of=javascript._is_test,
        call_nodes=frozenset({"call_expression"}),
        import_nodes=frozenset({"import_statement"}),
        import_of=javascript._import_of,
    )


GRAMMAR = _make_grammar("typescript", "language_typescript")
# `.tsx` is registered as its own language (`"tsx"`, see `cq/languages/__init__.py`)
# because JSX syntax needs the TSX-specific grammar factory.
TSX_GRAMMAR = _make_grammar("tsx", "language_tsx")


def extract_ex(source: str) -> tuple[tuple[RawSymbol, ...], str]:
    return _extract_ex(GRAMMAR, source)


def extract_ex_tsx(source: str) -> tuple[tuple[RawSymbol, ...], str]:
    return _extract_ex(TSX_GRAMMAR, source)


def _extract_ex(grammar: ts.Grammar, source: str) -> tuple[tuple[RawSymbol, ...], str]:
    """`(symbols, parser)`. Falls back to the regex extractor when the
    optional `tree-sitter-typescript` grammar is unavailable (FR-CQ-11)."""
    try:
        return ts.extract_with_fidelity(grammar, source)
    except (ExtractionError, ImportError):
        return extract_regex(source), "regex"


def extract(source: str) -> tuple[RawSymbol, ...]:
    return extract_ex(source)[0]


def extract_tsx(source: str) -> tuple[RawSymbol, ...]:
    return extract_ex_tsx(source)[0]


def chunk_spans(source: str, lines: list[str], max_chars: int) -> tuple[ChunkSpan, ...]:
    return ts.chunk_spans(GRAMMAR, source, lines, max_chars)


def chunk_spans_tsx(source: str, lines: list[str], max_chars: int) -> tuple[ChunkSpan, ...]:
    return ts.chunk_spans(TSX_GRAMMAR, source, lines, max_chars)


def extract_graph(source: str):
    try:
        return ts.extract_graph(GRAMMAR, source)
    except (ExtractionError, ImportError):
        return extract_graph_regex(source)


def extract_graph_tsx(source: str):
    try:
        return ts.extract_graph(TSX_GRAMMAR, source)
    except (ExtractionError, ImportError):
        return extract_graph_regex(source)


# --------------------------------------------------------------------------
# regex tier (fallback; shared by both "typescript" and "tsx")
# --------------------------------------------------------------------------

_INTERFACE_RE = re.compile(
    r"^\s*(?:export\s+)?(?:declare\s+)?interface\s+(?P<name>\w+)"
)
_TYPE_ALIAS_RE = re.compile(
    r"^\s*(?:export\s+)?(?:declare\s+)?type\s+(?P<name>\w+)\s*(?:<[^>]*>)?\s*="
)
_ENUM_RE = re.compile(
    r"^\s*(?:export\s+)?(?:declare\s+)?(?:const\s+)?enum\s+(?P<name>\w+)"
)
_ABSTRACT_CLASS_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?abstract\s+class\s+(?P<name>\w+)"
)
# A TypeScript method may carry a return type between `)` and `{`, and default
# parameter values put `=` inside the parameter list.
_ANNOTATED_METHOD_RE = re.compile(
    r"^\s*(?:(?:public|private|protected|readonly|static|abstract|async|override)\s+)*"
    r"(?:get\s+|set\s+)?(?P<name>[A-Za-z_#$][\w$]*)\s*(?:<[^>]*>)?\s*"
    r"\([^;]*\)\s*(?::\s*[^{;]+?)?\s*\{"
)
_SIGNATURE_RE = re.compile(
    r"^\s*(?:readonly\s+)?(?P<name>[A-Za-z_#$][\w$]*)\s*(?:<[^>]*>)?\s*"
    r"\([^;]*\)\s*:\s*[^{;]+;\s*$"
)

TS_RULES: tuple[javascript.Rule, ...] = (
    (_ABSTRACT_CLASS_RE, "class", True, False),
    (_INTERFACE_RE, "interface", True, False),
    (_ENUM_RE, "enum", True, False),
    (_TYPE_ALIAS_RE, "type", False, False),
    *javascript.JS_RULES,
    (_ANNOTATED_METHOD_RE, "method", False, True),
    (_SIGNATURE_RE, "method", False, True),
)


def extract_regex(source: str) -> tuple[RawSymbol, ...]:
    return javascript.scan(source, TS_RULES)


def extract_graph_regex(source: str):
    return javascript.extract_graph_regex(source)
