"""C# symbol extraction (FR-CQ-04 / FR-CQ-11).

The primary extractor is the official `tree-sitter-c-sharp` grammar (optional
dependency, `pip install -e .[code]`). A brace-aware regex extractor —
`extract_regex` / `extract_graph_regex` below — is kept as the fallback for
environments without the grammar installed, so this Skill still works without
it. `tree-sitter-language-pack` was evaluated and rejected: it downloads
grammars over the network on first use, which breaks the local-only guarantee
this Skill inherits from `markdown-query` and fails closed in offline agent
environments.
"""

from __future__ import annotations

import dataclasses
import re

from cq.languages import ChunkSpan, ExtractionError, RawSymbol, treesitter as ts
from cq.languages.linescan import brace_delta

# --------------------------------------------------------------------------
# tree-sitter tier (primary)
# --------------------------------------------------------------------------

# `record` has no kind of its own in the shared vocabulary (matches the regex
# tier's convention below), so it stays a class.
_TYPES = {
    "class_declaration": "class",
    "struct_declaration": "struct",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "class",
}

_XML_DOC_TAG = re.compile(r"</?summary>")


def _is_test(node, src, name: str) -> bool:
    return name.startswith("Test") or name.endswith("Tests")


def _import_of(node, src) -> str:
    """The last named child is always the actual imported path: a plain or
    `static` using has exactly one, an aliased `using X = Y;` has two (the
    alias name first, the target last)."""
    if not node.named_children:
        return ""
    target = node.named_children[-1]
    return ts.text(target, src).split(".")[0]


GRAMMAR = ts.Grammar(
    lang="csharp",
    module="tree_sitter_c_sharp",
    kinds={
        **_TYPES,
        "method_declaration": "function",  # promoted to "method" when bound to a type
        "constructor_declaration": "constructor",
    },
    scopes=_TYPES,
    name_of=lambda n, s: ts.field_name(n, s),
    scope_name_of=lambda n, s: ts.field_name(n, s),
    doc_markers=("///",),
    is_test_of=_is_test,
    call_nodes=frozenset({"invocation_expression"}),
    import_nodes=frozenset({"using_directive"}),
    import_of=_import_of,
)


def _clean_doc(symbol: RawSymbol) -> RawSymbol:
    """Strip the `<summary>` / `</summary>` XML doc tags the shared tree-sitter
    `doc_head()` helper does not know about (it only trims comment markers)."""
    if not symbol.doc_head:
        return symbol
    cleaned = _XML_DOC_TAG.sub("", symbol.doc_head).strip()
    if cleaned == symbol.doc_head:
        return symbol
    return dataclasses.replace(symbol, doc_head=cleaned or None)


def extract_ex(source: str) -> tuple[tuple[RawSymbol, ...], str]:
    """`(symbols, parser)`. Falls back to the regex extractor when the
    optional `tree-sitter-c-sharp` grammar is unavailable (FR-CQ-11)."""
    try:
        symbols, parser = ts.extract_with_fidelity(GRAMMAR, source)
    except (ExtractionError, ImportError):
        return extract_regex(source), "regex"
    return tuple(_clean_doc(s) for s in symbols), parser


def extract(source: str) -> tuple[RawSymbol, ...]:
    return extract_ex(source)[0]


def chunk_spans(source: str, lines: list[str], max_chars: int) -> tuple[ChunkSpan, ...]:
    """Structural chunks (FR-CQ-05); only available through tree-sitter. The
    regex tier never had a chunker (window chunks only), so an absent grammar
    still degrades exactly like before Phase 2 — the `ExtractionError`
    propagates and the core falls back to line-window chunks."""
    return ts.chunk_spans(GRAMMAR, source, lines, max_chars)


def extract_graph(source: str):
    try:
        return ts.extract_graph(GRAMMAR, source)
    except (ExtractionError, ImportError):
        return extract_graph_regex(source)


# --------------------------------------------------------------------------
# regex tier (fallback)
# --------------------------------------------------------------------------

_TYPE_RE = re.compile(
    r"^\s*(?:\[[^\]]*\]\s*)*"
    r"(?:(?:public|private|protected|internal|static|sealed|abstract|partial|readonly|new)\s+)*"
    r"(?P<kw>class|record|struct|interface|enum)\s+(?P<name>\w+)"
)
_METHOD_RE = re.compile(
    r"^\s*(?:\[[^\]]*\]\s*)*"
    r"(?:(?:public|private|protected|internal|static|async|override|virtual|sealed|"
    r"abstract|extern|partial|new|unsafe)\s+)+"
    r"(?P<ret>[\w<>,\[\]\?\.\s]+?)\s+(?P<name>\w+)\s*\("
)
_CTOR_RE = re.compile(
    r"^\s*(?:(?:public|private|protected|internal|static)\s+)+(?P<name>\w+)\s*\([^;]*\)\s*(?:\{|:|$)"
)
_DOC_RE = re.compile(r"^\s*///\s*(?:<summary>)?\s*(?P<text>[^<]+)")
_CONTROL_KEYWORDS = frozenset({"if", "for", "foreach", "while", "switch", "catch",
                               "using", "lock", "fixed", "return", "yield", "get", "set"})

# `record` has no kind of its own in the shared vocabulary, so it stays a class.
_KIND_BY_KEYWORD = {
    "class": "class",
    "record": "class",
    "struct": "struct",
    "interface": "interface",
    "enum": "enum",
}
_TYPE_KINDS = frozenset(_KIND_BY_KEYWORD.values())


def extract_regex(source: str) -> tuple[RawSymbol, ...]:
    lines = source.splitlines()
    found: list[RawSymbol] = []
    # (qualname, 宣言時の深さ, 本体の `{` を見たか)
    type_stack: list[list] = []
    depth = 0
    pending_doc: str | None = None

    for index, line in enumerate(lines):
        stripped = line.strip()
        doc = _DOC_RE.match(line)
        if doc:
            pending_doc = doc.group("text").strip() or pending_doc
        elif stripped and not stripped.startswith("//"):
            parent = type_stack[-1][0] if type_stack else None
            symbol = _match_symbol(line, index + 1, parent, pending_doc)
            if symbol is not None:
                found.append(symbol)
                if symbol.kind in _TYPE_KINDS:
                    type_stack.append([symbol.qualname, depth, False])
            pending_doc = None

        depth += brace_delta(line)
        # 宣言行の次に `{` が来る書き方があるため、本体を見るまで pop しない。
        for entry in type_stack:
            if not entry[2] and depth > entry[1]:
                entry[2] = True
        while type_stack and type_stack[-1][2] and depth <= type_stack[-1][1]:
            type_stack.pop()

    found.sort(key=lambda s: (s.start_line, s.qualname))
    return tuple(found)


def _match_symbol(
    line: str, number: int, parent: str | None, doc: str | None
) -> RawSymbol | None:
    type_match = _TYPE_RE.match(line)
    if type_match:
        kind = _KIND_BY_KEYWORD[type_match.group("kw")]
        return _make(type_match.group("name"), parent, kind, number, line, doc)

    method = _METHOD_RE.match(line)
    if method and method.group("name") not in _CONTROL_KEYWORDS:
        return _make(method.group("name"), parent, _kind(parent), number, line, doc)

    ctor = _CTOR_RE.match(line)
    if ctor and parent and ctor.group("name") == parent.rpartition(".")[2]:
        return _make(ctor.group("name"), parent, "method", number, line, doc)
    return None


def _kind(parent: str | None) -> str:
    return "method" if parent else "function"


def _make(
    name: str, parent: str | None, kind: str, number: int, line: str, doc: str | None
) -> RawSymbol:
    qualname = f"{parent}.{name}" if parent else name
    return RawSymbol(
        name=name,
        qualname=qualname,
        kind=kind,
        start_line=number,
        end_line=number,
        signature=line.strip().rstrip("{").strip()[:300],
        parent=parent,
        doc_head=doc,
        is_test=name.startswith("Test") or name.endswith("Tests"),
    )


_USING_RE = re.compile(r"^\s*using\s+(?:static\s+)?(?P<module>[\w.]+)\s*;")
_CALL_RE = re.compile(r"(?<![\w])(?P<name>[A-Za-z_]\w*)\s*\(")
_DECL_KEYWORDS = _CONTROL_KEYWORDS | {"new", "typeof", "nameof", "sizeof", "await", "throw"}


def extract_graph_regex(source: str) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """Return ``(references, imports)`` as ``(line, name)`` pairs."""
    declaration_lines = {s.start_line for s in extract_regex(source)}
    refs: list[tuple[int, str]] = []
    imports: list[tuple[int, str]] = []
    for number, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "///", "*", "/*")):
            continue
        using = _USING_RE.match(line)
        if using:
            imports.append((number, using.group("module").split(".")[0]))
            continue
        if number in declaration_lines:
            continue
        for match in _CALL_RE.finditer(line):
            name = match.group("name")
            if name not in _DECL_KEYWORDS:
                refs.append((number, name))
    return refs, imports
