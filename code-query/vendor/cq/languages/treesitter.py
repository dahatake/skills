"""Shared tree-sitter plumbing for the optional high-fidelity parsers (FR-CQ-11).

Each language contributes a :class:`Grammar` declaration and nothing else; the
walking, chunking and graph extraction below are language agnostic. The grammar
packages are optional (`pip install -e .[code]`), so every loader failure is
normalised to :class:`ExtractionError` and the indexer degrades to the regex
parser instead of failing.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Callable

from cq.chunking import span_size
from cq.languages import ChunkSpan, ExtractionError, RawSymbol

# Scope kinds that bind a function to a type. A function declared directly
# inside one of these becomes a `method`; `module` / `namespace` scopes do not
# promote, so a Rust `mod` function stays a function.
TYPE_SCOPE_KINDS = frozenset({"class", "struct", "interface", "enum", "impl", "type"})

_PARSERS: dict[tuple[str, str], object] = {}


def _no_decorators(node, src) -> tuple[str, ...]:
    return ()


def _not_test(node, src, name: str) -> bool:
    return False


def default_resolve(node, src, kind, name, scope, local_types):
    """Map a declaration onto (kind, local name, qualifying scope, type-bound)."""
    qual_scope = tuple(n for n, _ in scope)
    bound_to_type = bool(scope) and scope[-1][1] in TYPE_SCOPE_KINDS
    return kind, name, qual_scope, bound_to_type


@dataclass(frozen=True)
class Grammar:
    """Everything tree-sitter needs to know about one language."""

    lang: str
    module: str
    kinds: dict[str, str]
    scopes: dict[str, str]
    name_of: Callable
    scope_name_of: Callable
    doc_markers: tuple[str, ...]
    # Most grammar packages expose a single `language()` factory, but
    # `tree_sitter_typescript` exposes `language_typescript()` and
    # `language_tsx()` instead (no plain `language`), so the factory name is
    # configurable per declaration.
    language_func: str = "language"
    skip: frozenset[str] = frozenset()
    decorators_of: Callable = _no_decorators
    is_test_of: Callable = _not_test
    resolve: Callable = default_resolve
    signature_of: Callable | None = None
    sep: str = "."
    call_nodes: frozenset[str] = frozenset()
    callee_of: Callable | None = None
    import_nodes: frozenset[str] = frozenset()
    import_of: Callable | None = None


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def cache_key(grammar: Grammar) -> tuple[str, str]:
    """Identity of the compiled parser: the module plus the factory it calls.

    Not ``grammar.lang``: TypeScript and TSX are two ``lang`` identities backed
    by the same module (``tree_sitter_typescript``) but a different factory
    (``language_typescript`` / ``language_tsx``), so keying on ``lang`` alone
    would either collide or fail to distinguish them.
    """
    return (grammar.module, grammar.language_func)


def parser_for(grammar: Grammar):
    """Return a cached parser, importing the optional grammar on first use.

    A failure is cached too: without the optional extra, every file of that
    language would otherwise retry the same failing import.
    """
    key = cache_key(grammar)
    cached = _PARSERS.get(key)
    if isinstance(cached, ExtractionError):
        raise cached
    if cached is not None:
        return cached
    try:
        from tree_sitter import Language, Parser

        module = importlib.import_module(grammar.module)
        language_of = getattr(module, grammar.language_func)
        parser = Parser(Language(language_of()))
    except Exception as exc:  # ImportError, AttributeError, ValueError (ABI), OSError (loader)
        failure = ExtractionError(
            f"tree-sitter grammar unavailable for {grammar.lang}: {exc}"
        )
        _PARSERS[key] = failure
        raise failure from exc
    _PARSERS[key] = parser
    return parser


def parse(grammar: Grammar, source: str):
    return parser_for(grammar).parse(source.encode("utf-8"))


def text(node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", "replace")


def named_child_by_type(node, *types: str):
    for child in node.named_children:
        if child.type in types:
            return child
    return None


def descend_for(node, targets: frozenset[str], through: frozenset[str]):
    """Walk down *named* children looking for one of ``targets``.

    Field names are not dependable across grammars (`reference_declarator`
    exposes unnamed fields in tree-sitter-cpp), so named children are used.
    """
    if node is None:
        return None
    if node.type in targets:
        return node
    if node.type not in through:
        return None
    for child in node.named_children:
        found = descend_for(child, targets, through)
        if found is not None:
            return found
    return None


def field_name(node, src, fieldname: str = "name") -> str:
    target = node.child_by_field_name(fieldname)
    return text(target, src) if target is not None else ""


def doc_head(node, src, markers: tuple[str, ...]) -> str:
    """First meaningful line of the doc comment immediately above ``node``.

    Go's `// Ledger grants points.` sits above the `type_declaration` while the
    symbol node is the inner `type_spec`, so the search starts from the
    outermost ancestor that ends where ``node`` ends.
    """
    target = node
    while (
        target.parent is not None
        and target.parent.parent is not None
        and target.parent.end_byte == target.end_byte
    ):
        target = target.parent

    previous = target.prev_sibling
    while previous is not None and previous.type in ("attribute_item", "modifiers"):
        previous = previous.prev_sibling
    if previous is None or previous.type not in ("comment", "line_comment", "block_comment"):
        return ""
    body = text(previous, src).strip()
    if not body.startswith(markers):
        return ""
    for raw in body.splitlines():
        cleaned = raw.strip().lstrip("/*!").strip()
        if cleaned.endswith("*/"):
            cleaned = cleaned[:-2].rstrip().rstrip("*").rstrip()
        if cleaned:
            return cleaned
    return ""


def signature(node, src) -> str:
    """Declaration text up to the body. Grammars that mark the body with something
    other than a `body` field supply their own ``Grammar.signature_of``."""
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None else node.end_byte
    return " ".join(src[node.start_byte : end].decode("utf-8", "replace").split())[:200]


def line_range(node, last_line: int) -> tuple[int, int]:
    """Closed 1-based line range of ``node``, clamped to the file.

    A node that ends at column 0 (a preprocessor directive, or a file ending in a
    newline) reports the *following* row, which would make it claim a line it
    does not contain.
    """
    start = node.start_point[0] + 1
    end_row, end_column = node.end_point
    end = end_row + 1
    if end_column == 0 and end > start:
        end -= 1
    start = min(start, last_line)
    return start, min(max(end, start), last_line)


# --------------------------------------------------------------------------
# symbols
# --------------------------------------------------------------------------


def _declared_type_names(grammar: Grammar, node, src, acc: set[str]) -> None:
    if grammar.scopes.get(node.type) in TYPE_SCOPE_KINDS:
        name = grammar.scope_name_of(node, src)
        if name:
            acc.add(name)
    for child in node.named_children:
        _declared_type_names(grammar, child, src, acc)


def extract(grammar: Grammar, source: str) -> tuple[RawSymbol, ...]:
    return _symbols_from_tree(grammar, source, parse(grammar, source))


def extract_with_fidelity(grammar: Grammar, source: str) -> tuple[tuple[RawSymbol, ...], str]:
    """Like :func:`extract`, but also names the fidelity that was actually reached.

    ``tree-sitter-partial`` means the grammar produced at least one ``ERROR`` /
    missing node while parsing; declarations near the error may be missing or
    truncated even though the file did not degrade all the way to ``lite``
    (FR-CQ-11: the reduced fidelity must be visible to callers, not silently
    absorbed).
    """
    tree = parse(grammar, source)
    symbols = _symbols_from_tree(grammar, source, tree)
    parser = "tree-sitter-partial" if tree.root_node.has_error else "tree-sitter"
    return symbols, parser


def _symbols_from_tree(grammar: Grammar, source: str, tree) -> tuple[RawSymbol, ...]:
    src = source.encode("utf-8")
    last_line = max(len(source.splitlines()), 1)
    out: list[RawSymbol] = []
    local_types: set[str] = set()
    _declared_type_names(grammar, tree.root_node, src, local_types)

    def visit(node, scope: tuple[tuple[str, str], ...]) -> None:
        if node.type in grammar.skip:
            return
        kind = grammar.kinds.get(node.type)
        name = ""
        if kind is not None:
            name = grammar.name_of(node, src)
            if not name:
                kind = None

        next_scope = scope
        if kind is not None:
            kind, local, qual_scope, bound = grammar.resolve(
                node, src, kind, name, scope, local_types
            )
            if bound and kind in ("function", "prototype"):
                kind = "method"
            parent = grammar.sep.join(qual_scope)
            start, end = line_range(node, last_line)
            out.append(RawSymbol(
                name=local,
                qualname=grammar.sep.join((*qual_scope, local)),
                kind=kind,
                start_line=start,
                end_line=end,
                signature=(grammar.signature_of or signature)(node, src),
                parent=parent or None,
                doc_head=doc_head(node, src, grammar.doc_markers) or None,
                decorators=grammar.decorators_of(node, src),
                is_test=grammar.is_test_of(node, src, local),
            ))

        if node.type in grammar.scopes:
            scope_name = grammar.scope_name_of(node, src)
            if scope_name:
                next_scope = (*scope, (scope_name, grammar.scopes[node.type]))

        for child in node.named_children:
            visit(child, next_scope)

    visit(tree.root_node, ())
    out.sort(key=lambda s: (s.start_line, s.qualname, s.kind))
    return tuple(out)


# --------------------------------------------------------------------------
# chunks
# --------------------------------------------------------------------------


def chunk_spans(
    grammar: Grammar, source: str, lines: list[str], max_chars: int
) -> tuple[ChunkSpan, ...]:
    """Syntax-node chunk boundaries; oversized nodes recurse into children."""
    src = source.encode("utf-8")
    root = parse(grammar, source).root_node
    last = max(len(lines), 1)
    spans: list[ChunkSpan] = []

    def visit(node) -> None:
        start, end = line_range(node, last)
        declared = grammar.kinds.get(node.type) is not None
        name = grammar.name_of(node, src) if declared else ""
        if span_size(lines, start, end) <= max_chars or not node.named_children:
            signature_text = (grammar.signature_of or signature)(node, src) if name else ""
            spans.append(ChunkSpan(start, end, name, signature_text))
            return
        for child in node.named_children:
            visit(child)

    for child in root.named_children:
        visit(child)
    return tuple(spans)


# --------------------------------------------------------------------------
# graph (FR-CQ-07)
# --------------------------------------------------------------------------

_CALLEE_NAMES = frozenset({"identifier", "field_identifier", "type_identifier"})


def _callee(node, src) -> str:
    """Callee name for grammars whose call node points at it through a field.

    Grammars that expose the name as a named child instead supply their own
    ``Grammar.callee_of``.
    """
    target = node.child_by_field_name("function") or node.child_by_field_name("name")
    if target is None:
        return ""
    while target is not None and target.named_children:
        if target.type in _CALLEE_NAMES:
            break
        tail = target.named_children[-1]
        if tail.type in ("argument_list", "template_argument_list", "type_arguments"):
            break
        target = tail
    return text(target, src) if target is not None else ""


def extract_graph(grammar: Grammar, source: str):
    src = source.encode("utf-8")
    root = parse(grammar, source).root_node
    refs: list[tuple[int, str]] = []
    imports: list[tuple[int, str]] = []

    def visit(node) -> None:
        if node.type in grammar.call_nodes:
            name = (grammar.callee_of or _callee)(node, src)
            if name:
                refs.append((node.start_point[0] + 1, name))
        elif node.type in grammar.import_nodes and grammar.import_of is not None:
            module = grammar.import_of(node, src)
            if module:
                imports.append((node.start_point[0] + 1, module))
        for child in node.named_children:
            visit(child)

    visit(root)
    return tuple(refs), tuple(imports)
