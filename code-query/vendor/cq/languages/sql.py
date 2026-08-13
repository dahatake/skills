"""SQL symbol extraction (FR-CQ-04 / FR-CQ-05 / FR-CQ-11).

主エンジンは sqlglot。方言は固定順で試し、全文を構造化できた最初の方言を採用する
ので同一入力に対する結果は決定的になる。個別方言がいずれも構造化できない場合だけ、
最後に方言を指定しない解析を 1 回試す（sqlglot は方言無指定を全方言のスーパーセット
として扱う。公式 FAQ）。

sqlglot がファイルを構造化できない場合と、ストアド／関数の本体を構造化できていない
場合だけ sqlfluff へエスカレーションする。sqlglot はどの方言でも Oracle PL/SQL の
本体を構造化できず（bigquery 方言がルーチン名だけ拾って通してしまう）、PostgreSQL の
`$$` 本体も 1 トークンとして扱うため、本体の参照は sqlfluff でしか取れない。
sqlfluff は 1 文あたり数十 ms かかるので、この 2 条件以外では起動しない。

`$tag$ ... $tag$` の本体だけは sqlfluff でも 1 つのリテラルになるため、tree-sitter の SQL
文法で再パースして本体内のテーブル参照を拾う。

sqlfluff は任意依存（`pip install -e .[code,code-sql]`）。未導入なら sqlglot の
結果をそのまま使う。
"""

from __future__ import annotations

import contextlib
import functools
import logging
import re
from typing import NamedTuple

from cq.languages import ChunkSpan, ExtractionError, RawSymbol

# 最小 kind 語彙へ写せる CREATE 種別だけを索引する。
_KINDS = {
    "PROCEDURE": "function",
    "FUNCTION": "function",
    "VIEW": "struct",
    "TABLE": "struct",
    "SCHEMA": "namespace",
}

# 順序は固定（同一入力に対する結果を決定的にするため）。既存 5 方言の後ろに追加する。
_DIALECTS = (
    "tsql", "oracle", "postgres", "bigquery", "spark",
    "mysql", "sqlite", "snowflake", "duckdb",
)
_FLUFF_DIALECTS = ("tsql", "oracle", "postgres", "bigquery", "sparksql")

_FLUFF_KINDS = {
    "create_table_statement": "struct",
    "create_view_statement": "struct",
    "create_procedure_statement": "function",
    "create_function_statement": "function",
    "create_schema_statement": "namespace",
}
# 定義された対象の名前は、文の中で最初に現れるこれらの segment。
_FLUFF_NAMES = ("table_reference", "object_reference", "function_name", "procedure_name")
_QUOTES = re.compile(r"[`\"\[\]]")
# `$fn$` / `$$` の開始タグ。PostgreSQL の dollar-quoted ルーチン本体。
_DOLLAR_TAG = re.compile(r"^\$[A-Za-z_][A-Za-z0-9_]*\$|^\$\$")
# 再パースした本体で、object_reference が関係（テーブル）を指す親ノード型。
_RELATION_PARENTS = frozenset({"relation", "from", "insert", "update"})

# `GO` は T-SQL のバッチ区切りであって文ではない。tokenizer は `GO` 以降を丸ごと
# COMMAND として飲み込むため、行・桁・バイト位置を保ったまま `;` へ置き換える。
_BATCH = re.compile(r"^([ \t]*)GO([ \t]*)$", re.IGNORECASE | re.MULTILINE)


class _Unit(NamedTuple):
    """One SQL statement, normalised across the two engines."""

    start: int
    end: int
    kind: str  # 索引対象の定義でなければ ""
    name: str
    qualname: str
    signature: str
    refs: tuple[tuple[int, str], ...]


# --------------------------------------------------------------------------
# sqlglot
# --------------------------------------------------------------------------


@contextlib.contextmanager
def _quiet():
    """未対応構文は戻り値の `Command` で判定するので、その警告ログは出さない。"""
    logger = logging.getLogger("sqlglot")
    previous = logger.level
    logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        logger.setLevel(previous)


def _span(statement) -> tuple[int, int] | None:
    """Line range of a statement. sqlglot records positions on terminal nodes only,
    so a trailing `END;` or `)` that carries no identifier is not counted."""
    lines = [
        node.meta["line"]
        for node in statement.walk()
        if getattr(node, "meta", None) and "line" in node.meta
    ]
    return (min(lines), max(lines)) if lines else None


def _created(node):
    """Table node naming the object a CREATE statement defines."""
    from sqlglot import exp

    target = node.this
    if target is None:
        return None
    return target if isinstance(target, exp.Table) else target.find(exp.Table)


def _qualname(table) -> str:
    return ".".join(part for part in (table.catalog, table.db, table.name) if part)


def _table_line(table, fallback: int) -> int:
    from sqlglot import exp

    identifier = table.this if isinstance(table.this, exp.Identifier) else table.find(exp.Identifier)
    return (identifier.meta.get("line") if identifier is not None else None) or fallback


def _glot_unit(node, start: int, end: int, dialect: str | None) -> _Unit:
    from sqlglot import exp

    created = _created(node) if isinstance(node, exp.Create) else None
    refs = tuple(
        (_table_line(table, start), table.name)
        for table in node.find_all(exp.Table)
        if table is not created and table.name
    )
    kind = ""
    name = qualname = signature = ""
    if isinstance(node, exp.Create) and created is not None and created.name:
        kind = _KINDS.get(str(node.args.get("kind") or "").upper(), "")
        if kind:
            name, qualname = created.name, _qualname(created)
            signature = " ".join(
                f"CREATE {node.args['kind']} {node.this.sql(dialect=dialect)}".split()
            )[:200]
    return _Unit(start, end, kind, name, qualname, signature, refs)


def _try_dialect(neutralised: str, dialect: str | None) -> tuple[_Unit, ...] | None:
    """One parse attempt; ``None`` when ``dialect`` cannot structure the whole input."""
    import sqlglot
    from sqlglot import exp

    try:
        with _quiet():
            statements = sqlglot.parse(neutralised, dialect=dialect)
    except Exception:  # noqa: BLE001  tokenizer / parser errors are dialect specific
        return None
    units: list[_Unit] = []
    for node in statements:
        if node is None:
            continue
        if isinstance(node, exp.Command):
            # 未対応構文は Command へ退避される。構造化できていないので
            # 「この方言で解析できた」とは扱わない。
            return None
        span = _span(node)
        if span is not None:
            units.append(_glot_unit(node, span[0], span[1], dialect))
    return tuple(units) if units else None


def _by_sqlglot(source: str) -> tuple[_Unit, ...]:
    neutralised = _BATCH.sub(lambda m: f"{m.group(1)}; {m.group(2)}", source)
    for dialect in _DIALECTS:
        units = _try_dialect(neutralised, dialect)
        if units is not None:
            return units
    # 個別方言がいずれも通らないときだけ、全方言のスーパーセットである sqlglot 既定
    # （方言無指定）でもう一度だけ試す（sqlglot 公式 FAQ: 方言無指定は全方言の
    # スーパーセットとして解析される）。試行順は固定なので結果は決定的なまま。
    units = _try_dialect(neutralised, None)
    if units is not None:
        return units
    raise ExtractionError("no sqlglot dialect could structure this SQL")


# --------------------------------------------------------------------------
# sqlfluff (escalation)
# --------------------------------------------------------------------------


@functools.lru_cache(maxsize=len(_FLUFF_DIALECTS))
def _linter(dialect: str):
    from sqlfluff.core import FluffConfig, Linter

    return Linter(config=FluffConfig(overrides={"dialect": dialect, "rules": "none"}))


@functools.lru_cache(maxsize=1)
def _embedded_parser():
    """`None` when the optional grammar is absent; the failure is cached too."""
    try:
        import tree_sitter
        import tree_sitter_sql

        return tree_sitter.Parser(tree_sitter.Language(tree_sitter_sql.language()))
    except Exception:  # noqa: BLE001  ImportError, ValueError (ABI), OSError (loader)
        return None


def _embedded_refs(body: str) -> list[tuple[int, str]]:
    """Table references inside a routine body, by line within ``body``.

    The grammar recovers the embedded statements even though the surrounding
    procedural syntax does not parse. Only the parents below name a relation:
    the same node type also carries column qualifiers (`m` in `m.id`) and
    function names.
    """
    parser = _embedded_parser()
    if parser is None:
        return []
    src = body.encode("utf-8")
    found: list[tuple[int, str]] = []
    stack = [parser.parse(src).root_node]
    while stack:
        node = stack.pop()
        if node.type == "object_reference" and node.parent is not None and node.parent.type in _RELATION_PARENTS:
            name = src[node.start_byte : node.end_byte].decode("utf-8", "replace")
            found.append((node.start_point[0] + 1, name.split(".")[-1]))
        stack.extend(node.children)
    return sorted(set(found))


def _dollar_body_refs(statement) -> list[tuple[int, str]]:
    """References hidden in `$tag$ ... $tag$` bodies, by line within the file."""
    out: list[tuple[int, str]] = []
    for literal in statement.recursive_crawl("quoted_literal"):
        raw = literal.raw
        opening = _DOLLAR_TAG.match(raw)
        if opening is None:
            continue
        tag = opening.group(0)
        body = raw[len(tag) : -len(tag)] if raw.endswith(tag) else raw[len(tag) :]
        start = literal.pos_marker.working_line_no
        out.extend((start + line - 1, name) for line, name in _embedded_refs(body))
    return out


def _fluff_unit(statement) -> _Unit:
    body = statement.segments[0]
    start = statement.pos_marker.working_line_no
    end = start + statement.raw.rstrip().count("\n")
    kind = _FLUFF_KINDS.get(body.get_type(), "")
    defined = next(body.recursive_crawl(*_FLUFF_NAMES), None) if kind else None
    refs = [
        (segment.pos_marker.working_line_no, _QUOTES.sub("", segment.raw).split(".")[-1])
        for segment in body.recursive_crawl("table_reference")
        if segment is not defined
    ]
    refs.extend(_dollar_body_refs(body))
    refs.sort()
    if defined is None:
        return _Unit(start, end, "", "", "", "", tuple(refs))
    qualname = _QUOTES.sub("", defined.raw)
    return _Unit(
        start,
        end,
        kind,
        qualname.split(".")[-1],
        qualname,
        " ".join(statement.raw.splitlines()[0].split())[:200],
        tuple(refs),
    )


def _by_sqlfluff(source: str) -> tuple[_Unit, ...] | None:
    """`None` when sqlfluff is absent or no dialect parses ``source`` cleanly."""
    try:
        import sqlfluff.core  # noqa: F401  `code-sql` extra の導入確認
    except ImportError:
        return None
    for dialect in _FLUFF_DIALECTS:
        try:
            tree = _linter(dialect).parse_string(source).tree
        except Exception:  # noqa: BLE001  dialect specific parse failures
            continue
        if tree is None or next(tree.recursive_crawl("unparsable"), None) is not None:
            continue
        units = tuple(
            _fluff_unit(statement)
            for statement in tree.recursive_crawl("statement", recurse_into=False)
        )
        if units:
            return units
    return None


# --------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------


def _body_is_opaque(units: tuple[_Unit, ...]) -> bool:
    """A routine that reports no reference may simply not have been parsed."""
    return any(unit.kind == "function" and not unit.refs for unit in units)


@functools.lru_cache(maxsize=1)
def _analyse(source: str) -> tuple[_Unit, ...]:
    """Units for ``source``. Cached because the indexer asks three times per file."""
    try:
        units = _by_sqlglot(source)
    except ExtractionError:
        escalated = _by_sqlfluff(source)
        if escalated is None:
            raise
        return escalated
    if _body_is_opaque(units):
        return _by_sqlfluff(source) or units
    return units


def extract(source: str) -> tuple[RawSymbol, ...]:
    out = [
        RawSymbol(
            name=unit.name,
            qualname=unit.qualname,
            kind=unit.kind,
            start_line=unit.start,
            end_line=unit.end,
            signature=unit.signature,
        )
        for unit in _analyse(source)
        if unit.kind
    ]
    out.sort(key=lambda s: (s.start_line, s.qualname, s.kind))
    return tuple(out)


def chunk_spans(source: str, lines: list[str], max_chars: int) -> tuple[ChunkSpan, ...]:
    """One span per statement; the core fills the gaps between them."""
    return tuple(
        ChunkSpan(unit.start, unit.end, unit.qualname, unit.signature)
        for unit in _analyse(source)
    )


def extract_graph(source: str):
    refs = tuple(ref for unit in _analyse(source) for ref in unit.refs)
    return refs, ()
