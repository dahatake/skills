"""Python symbol extraction via the standard library `ast` (FR-CQ-04).

The HVE application itself — 644 tracked Python files — is always indexable at
full fidelity through `ast`, so no third-party parser is required for it.

A file that does not parse with `ast` (typically one being edited and
momentarily invalid) falls back to the optional `tree-sitter-python` grammar
(FR-CQ-11) instead of dropping straight to the line-window `lite` fidelity.
The fallback cannot recover Python's docstrings — they are a body statement,
not a comment the shared tree-sitter `doc_head()` helper looks for — so `doc_head`
is unavailable for this tier; declarations, line ranges, decorators and
references still are.
"""

from __future__ import annotations

import ast

from cq.languages import ChunkSpan, RawSymbol, treesitter as ts

_DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def extract(source: str) -> tuple[RawSymbol, ...]:
    return extract_ex(source)[0]


def extract_ex(source: str) -> tuple[tuple[RawSymbol, ...], str]:
    """`(symbols, parser)`. Falls back to tree-sitter when `ast` cannot parse
    ``source`` instead of raising straight to the `lite` degradation (FR-CQ-11).
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return ts.extract_with_fidelity(GRAMMAR, source)

    found: list[RawSymbol] = []
    _walk(tree.body, parent=None, in_class=False, inherited_test=False, out=found)
    found.sort(key=lambda s: (s.start_line, s.qualname, s.kind))
    return tuple(found), "ast"


def _walk(
    body: list[ast.stmt],
    *,
    parent: str | None,
    in_class: bool,
    inherited_test: bool,
    out: list[RawSymbol],
) -> None:
    for node in body:
        if not isinstance(node, _DEF_NODES):
            continue
        qualname = f"{parent}.{node.name}" if parent else node.name
        is_class = isinstance(node, ast.ClassDef)
        is_test = inherited_test or _looks_like_test(node.name)
        out.append(RawSymbol(
            name=node.name,
            qualname=qualname,
            kind="class" if is_class else ("method" if in_class else "function"),
            start_line=node.lineno,
            end_line=getattr(node, "end_lineno", node.lineno) or node.lineno,
            signature=_signature(node),
            parent=parent,
            doc_head=_doc_head(node),
            decorators=tuple(_decorator_name(d) for d in node.decorator_list),
            is_test=is_test,
        ))
        _walk(
            node.body,
            parent=qualname,
            in_class=is_class,
            inherited_test=is_test,
            out=out,
        )


def _looks_like_test(name: str) -> bool:
    return name.startswith("test_") or name.startswith("Test")


def _signature(node: ast.stmt) -> str:
    if isinstance(node, ast.ClassDef):
        bases = [ast.unparse(b) for b in node.bases]
        bases += [f"{kw.arg}={ast.unparse(kw.value)}" for kw in node.keywords if kw.arg]
        return f"class {node.name}({', '.join(bases)})" if bases else f"class {node.name}"
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    return f"{prefix} {node.name}({ast.unparse(node.args)}){returns}"


def _doc_head(node: ast.stmt) -> str | None:
    doc = ast.get_docstring(node)
    if not doc:
        return None
    # A docstring can be non-empty yet contain no printable line (numpy has one),
    # in which case indexing `splitlines()[0]` would raise IndexError.
    for line in doc.strip().splitlines():
        head = line.strip()
        if head:
            return head
    return None


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    try:
        return ast.unparse(node)
    except Exception:
        return "<unparsable>"


def chunk_spans(source: str, lines: list[str], max_chars: int) -> tuple[ChunkSpan, ...]:
    """Structural chunk boundaries for Python (FR-CQ-05).

    Definitions are the unit; a definition larger than ``max_chars`` is split
    into its own header plus its body statements. Falls back to tree-sitter
    chunk spans when `ast` cannot parse ``source`` (FR-CQ-11); if that grammar
    is also unavailable, the `ExtractionError` propagates so the core degrades
    to line-window chunks instead.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return ts.chunk_spans(GRAMMAR, source, lines, max_chars)
    return tuple(
        _spans(tree.body, lines, max_chars, start_line=1, end_line=len(lines))
    )


def _spans(
    body: list[ast.stmt], lines: list[str], max_chars: int, *, start_line: int, end_line: int
) -> list[ChunkSpan]:
    """Cover ``start_line..end_line`` exactly once, splitting oversized definitions."""
    spans: list[ChunkSpan] = []
    cursor = start_line
    for node in body:
        start = getattr(node, "lineno", cursor)
        end = getattr(node, "end_lineno", start) or start
        decorators = getattr(node, "decorator_list", [])
        if decorators:
            start = min(start, min(d.lineno for d in decorators))
        if start > cursor:
            spans.append(ChunkSpan(cursor, start - 1))
        if isinstance(node, _DEF_NODES):
            spans.extend(_split_definition(node, start, end, lines, max_chars))
        else:
            spans.append(ChunkSpan(start, end))
        cursor = end + 1
    if cursor <= end_line:
        spans.append(ChunkSpan(cursor, end_line))
    return spans


def _split_definition(
    node: ast.stmt, start: int, end: int, lines: list[str], max_chars: int
) -> list[ChunkSpan]:
    name = node.name
    signature = lines[start - 1].strip() if 0 < start <= len(lines) else ""
    size = sum(len(lines[i]) + 1 for i in range(start - 1, min(end, len(lines))))
    if size <= max_chars or not getattr(node, "body", None):
        return [ChunkSpan(start, end, name, signature)]
    header_end = max(start, min(node.body[0].lineno - 1, end))
    spans = [ChunkSpan(start, header_end, name, signature)]
    spans.extend(_spans(node.body, lines, max_chars, start_line=header_end + 1, end_line=end))
    return spans


# --------------------------------------------------------------------------
# tree-sitter fallback (FR-CQ-11 Phase 5): only reached when `ast` cannot
# parse the file. `cq/graph.py` special-cases Python outside the generic
# `LanguageSupport.graph` dispatch, so this is consulted there directly.
# --------------------------------------------------------------------------


def _decorators_of(node, src) -> tuple[str, ...]:
    """A decorated `def`/`class` is wrapped in a `decorated_definition` parent
    whose preceding `decorator` children apply to it (unlike Java/Rust, which
    attach decorators as a sibling or same-node child)."""
    parent = node.parent
    if parent is None or parent.type != "decorated_definition":
        return ()
    return tuple(
        " ".join(ts.text(child, src).split())
        for child in parent.named_children
        if child.type == "decorator"
    )


def _is_test(node, src, name: str) -> bool:
    return _looks_like_test(name)


def _import_of(node, src) -> str:
    """First path component, matching `graph.extract_python`'s AST-based
    convention. A relative import's leading dots (`.`, `.pkg`) are stripped
    before splitting so a bare `from . import x` yields no module (the AST
    path records `module=None` for it too)."""
    field = "name" if node.type == "import_statement" else "module_name"
    target = node.child_by_field_name(field)
    if target is None:
        return ""
    module = ts.text(target, src).lstrip(".")
    return module.split(".")[0] if module else ""


GRAMMAR = ts.Grammar(
    lang="python",
    module="tree_sitter_python",
    kinds={
        "class_definition": "class",
        "function_definition": "function",
    },
    scopes={"class_definition": "class"},
    name_of=lambda n, s: ts.field_name(n, s),
    scope_name_of=lambda n, s: ts.field_name(n, s),
    # Docstrings are a body statement, not a preceding comment; no marker
    # makes `doc_head()` a safe, correct no-op for this tier (see module doc).
    doc_markers=(),
    decorators_of=_decorators_of,
    is_test_of=_is_test,
    call_nodes=frozenset({"call"}),
    import_nodes=frozenset({"import_statement", "import_from_statement"}),
    import_of=_import_of,
)


def extract_graph(source: str):
    return ts.extract_graph(GRAMMAR, source)

