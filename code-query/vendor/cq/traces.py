"""Traceability between source code and design documents (FR-CQ-07).

The repository already annotates generated code with `出典:` comments pointing at
`docs/**` anchors, and normative requirement IDs appear inline. Those are the join
keys between `cq` (code) and `mdq` (documents); this module owns the single
definition of the patterns so that no consumer re-declares them.

`cq` only resolves *locations*. Fetching the document body is `mdq`'s job.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from cq import store

# `hve-dev/generate_tdd_inventory.py` はこの定義を import して使う（二重定義の禁止）。
FEATURE_ID_RE = r"(?:FR|NFR)-[A-Z0-9]+(?:-[A-Z0-9]+)*(?:-§[0-9.]+)?|C-\d+|UC-\d+"
TEST_ID_RE = r"(?:TEST|UT|IT|E2E)-[A-Z0-9]+(?:-[A-Z0-9]+)*"
SCOPE_ID_RE = r"APP-\d{3}|SVC-\d{2}"

_SOURCE_REF_RE = re.compile(
    r"出典:\s*(?P<doc>[\w./\-]+\.md)(?:#(?P<anchor>[\w.\-]+))?"
)
_BARE_ID_RE = re.compile(rf"\b(?:{FEATURE_ID_RE}|{TEST_ID_RE}|{SCOPE_ID_RE})\b")


@dataclass(frozen=True)
class Trace:
    line: int
    trace_id: str
    doc_path: str | None = None
    anchor: str | None = None


def extract(source: str) -> tuple[Trace, ...]:
    """Return every traceability marker in ``source``, ordered by line."""
    found: list[Trace] = []
    for number, line in enumerate(source.splitlines(), start=1):
        reference = _SOURCE_REF_RE.search(line)
        if reference and reference.group("anchor"):
            found.append(Trace(
                line=number,
                trace_id=reference.group("anchor"),
                doc_path=reference.group("doc"),
                anchor=f"#{reference.group('anchor')}",
            ))
            continue
        seen: set[str] = set()
        for match in _BARE_ID_RE.finditer(line):
            identifier = match.group(0)
            if identifier in seen:
                continue
            seen.add(identifier)
            doc_path = reference.group("doc") if reference else None
            found.append(Trace(line=number, trace_id=identifier, doc_path=doc_path))
    return tuple(found)


def for_path(db_path: Path, path: str) -> list[dict[str, object]]:
    """Reverse lookup: which design documents does this file cite?"""
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT path, line, trace_id, doc_path, anchor FROM traces"
            " WHERE path = ? AND doc_path IS NOT NULL ORDER BY line",
            (path,),
        ).fetchall()
    return [dict(row) for row in rows]


def references(db_path: Path, name: str, *, top_k: int = 20) -> list[dict[str, object]]:
    """Where is ``name`` referenced?"""
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT path, line, name FROM refs WHERE name = ?"
            " ORDER BY path, line LIMIT ?",
            (name, top_k),
        ).fetchall()
    return [dict(row) for row in rows]


def _connect(db_path: Path) -> sqlite3.Connection:
    return store.open_store(db_path, create=False)
