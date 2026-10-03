"""JavaScript symbol extraction (FR-CQ-04 / FR-CQ-11).

The primary extractor is the official `tree-sitter-javascript` grammar
(optional dependency, `pip install -e .[code]`). A brace-aware regex
extractor — `extract_regex` / `extract_graph_regex` below, also shared with
TypeScript's own fallback via `scan()` — is kept for environments without the
grammar installed. `require(...)` (CommonJS) calls are not recognised as
imports by the tree-sitter tier, only ECMAScript `import` statements; the
regex tier still recognises both.
"""

from __future__ import annotations

import re

from cq.languages import ChunkSpan, ExtractionError, RawSymbol, treesitter as ts
from cq.languages.linescan import brace_delta

# --------------------------------------------------------------------------
# tree-sitter tier (primary)
# --------------------------------------------------------------------------

_FUNCTION_VALUED = frozenset({"function_expression", "arrow_function", "generator_function"})
# テストブロックの先頭語。Jest / Mocha / Vitest / Playwright はこの 3 語を共有する。
_TEST_CALLEES = frozenset({"describe", "it", "test"})


def _test_block_label(node, src) -> str:
    """Label of a `describe(...)` / `it(...)` / `test(...)` statement, else `""`.

    The node is the wrapping `expression_statement`, not the call: chunk naming
    only consults nodes listed in `Grammar.kinds`, and it stops descending once a
    node fits the size budget. Keying on the call itself would produce the symbol
    but leave the chunk's `name` / `signature` columns empty, which are the two
    highest BM25 weights.
    """
    call = next((c for c in node.named_children if c.type == "call_expression"), None)
    if call is None:
        return ""
    callee = call.child_by_field_name("function")
    if callee is not None and callee.type == "member_expression":  # `it.skip(...)`
        callee = callee.child_by_field_name("object")
    if callee is None or callee.type != "identifier":
        return ""
    if ts.text(callee, src) not in _TEST_CALLEES:
        return ""
    arguments = call.child_by_field_name("arguments")
    if arguments is None:
        return ""
    for argument in arguments.named_children:
        # ラベルは必ず第 1 引数。そうでなければテストブロックではない。
        return ts.text(argument, src)[1:-1] if argument.type in ("string", "template_string") else ""
    return ""


def _name_of(node, src) -> str:
    """`variable_declarator` covers every `const/let/var` binding, so only a
    function-valued, non-destructured one is a symbol (`const x = 5` and
    `const { a } = obj` are not)."""
    if node.type == "expression_statement":
        return _test_block_label(node, src)
    if node.type != "variable_declarator":
        return ts.field_name(node, src)
    value = node.child_by_field_name("value")
    if value is None or value.type not in _FUNCTION_VALUED:
        return ""
    target = node.child_by_field_name("name")
    return ts.text(target, src) if target is not None and target.type == "identifier" else ""


def _is_test(node, src, name: str) -> bool:
    if node.type == "expression_statement":
        return True  # `_name_of` がテストブロック以外を既に除外している。
    return name.startswith("test") or name.startswith("Test")


def _import_of(node, src) -> str:
    target = node.child_by_field_name("source")
    return ts.text(target, src).strip("'\"") if target is not None else ""


GRAMMAR = ts.Grammar(
    lang="javascript",
    module="tree_sitter_javascript",
    kinds={
        "class_declaration": "class",
        "function_declaration": "function",
        "generator_function_declaration": "function",
        # A class/object-literal method either way, so this maps directly to
        # "method" rather than relying on the class-scope promotion.
        "method_definition": "method",
        "variable_declarator": "function",  # only fires per `_name_of` above
        "expression_statement": "function",  # only fires for test blocks per `_name_of`
    },
    scopes={"class_declaration": "class"},
    name_of=_name_of,
    scope_name_of=lambda n, s: ts.field_name(n, s),
    doc_markers=("/**",),
    is_test_of=_is_test,
    call_nodes=frozenset({"call_expression"}),
    import_nodes=frozenset({"import_statement"}),
    import_of=_import_of,
)


def extract_ex(source: str) -> tuple[tuple[RawSymbol, ...], str]:
    """`(symbols, parser)`. Falls back to the regex extractor when the
    optional `tree-sitter-javascript` grammar is unavailable (FR-CQ-11)."""
    try:
        return ts.extract_with_fidelity(GRAMMAR, source)
    except (ExtractionError, ImportError):
        return extract_regex(source), "regex"


def extract(source: str) -> tuple[RawSymbol, ...]:
    return extract_ex(source)[0]


def chunk_spans(source: str, lines: list[str], max_chars: int) -> tuple[ChunkSpan, ...]:
    """Structural chunks (FR-CQ-05); only available through tree-sitter. The
    regex tier never had a chunker, so an absent grammar still degrades
    exactly like before Phase 2 (line-window chunks)."""
    return ts.chunk_spans(GRAMMAR, source, lines, max_chars)


def extract_graph(source: str):
    try:
        return ts.extract_graph(GRAMMAR, source)
    except (ExtractionError, ImportError):
        return extract_graph_regex(source)


# --------------------------------------------------------------------------
# regex tier (fallback)
# --------------------------------------------------------------------------

_CLASS_RE = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+(?P<name>\w+)")
_FUNCTION_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(?P<name>\w+)\s*\("
)
_ASSIGNED_FN_RE = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>\w+)\s*=\s*"
    r"(?:async\s*)?(?:function\s*\*?\s*\w*\s*\(|\([^)]*\)\s*=>|\w+\s*=>)"
)
_METHOD_RE = re.compile(
    r"^\s*(?:static\s+)?(?:async\s+)?(?:get\s+|set\s+)?(?P<name>[A-Za-z_#$][\w$]*)\s*\([^;=]*\)\s*\{"
)
_CONTROL_KEYWORDS = frozenset({
    "if", "for", "while", "switch", "catch", "return", "function", "class",
    "do", "else", "try", "finally", "with", "constructor",
})

# (pattern, kind, opens a scope, requires an enclosing scope)
Rule = tuple[re.Pattern, str, bool, bool]

JS_RULES: tuple[Rule, ...] = (
    (_CLASS_RE, "class", True, False),
    (_FUNCTION_RE, "function", False, False),
    (_ASSIGNED_FN_RE, "function", False, False),
    (_METHOD_RE, "method", False, True),
)


def extract_regex(source: str) -> tuple[RawSymbol, ...]:
    return scan(source, JS_RULES)


def scan(source: str, rules: tuple[Rule, ...]) -> tuple[RawSymbol, ...]:
    lines = source.splitlines()
    found: list[RawSymbol] = []
    scope_stack: list[list] = []  # (qualname, 宣言時の深さ, 本体を見たか)
    depth = 0

    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith(("//", "*", "/*")):
            parent = scope_stack[-1][0] if scope_stack else None
            symbol, opens_scope = _match_symbol(line, index + 1, parent, rules)
            if symbol is not None:
                found.append(symbol)
                if opens_scope:
                    scope_stack.append([symbol.qualname, depth, False])

        depth += brace_delta(line)
        for entry in scope_stack:
            if not entry[2] and depth > entry[1]:
                entry[2] = True
        while scope_stack and scope_stack[-1][2] and depth <= scope_stack[-1][1]:
            scope_stack.pop()

    found.sort(key=lambda s: (s.start_line, s.qualname))
    return tuple(found)


def _match_symbol(
    line: str, number: int, parent: str | None, rules: tuple[Rule, ...]
) -> tuple[RawSymbol | None, bool]:
    for pattern, kind, opens_scope, needs_parent in rules:
        if needs_parent and parent is None:
            continue
        match = pattern.match(line)
        if match and match.group("name") not in _CONTROL_KEYWORDS:
            return _make(match.group("name"), parent, kind, number, line), opens_scope
    return None, False


def _make(name: str, parent: str | None, kind: str, number: int, line: str) -> RawSymbol:
    qualname = f"{parent}.{name}" if parent else name
    return RawSymbol(
        name=name,
        qualname=qualname,
        kind=kind,
        start_line=number,
        end_line=number,
        signature=line.strip().rstrip("{").strip()[:300],
        parent=parent,
        is_test=name.startswith("test") or name.startswith("Test"),
    )


_IMPORT_RE = re.compile(r"""^\s*(?:import\b[^'"]*from\s*|import\s*|.*\brequire\s*\()['"](?P<module>[^'"]+)['"]""")
_CALL_RE = re.compile(r"(?<![\w$])(?P<name>[A-Za-z_$][\w$]*)\s*\(")
_DECL_KEYWORDS = _CONTROL_KEYWORDS | {"new", "typeof", "await", "throw", "import", "require"}


def extract_graph_regex(source: str) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """Return ``(references, imports)`` as ``(line, name)`` pairs."""
    declaration_lines = {s.start_line for s in extract_regex(source)}
    refs: list[tuple[int, str]] = []
    imports: list[tuple[int, str]] = []
    for number, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "*", "/*")):
            continue
        module = _IMPORT_RE.match(line)
        if module:
            imports.append((number, module.group("module")))
            continue
        if number in declaration_lines:
            continue
        for match in _CALL_RE.finditer(line):
            name = match.group("name")
            if name not in _DECL_KEYWORDS:
                refs.append((number, name))
    return refs, imports
