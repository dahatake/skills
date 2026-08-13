"""SQLite-backed index store for cq (FR-CQ-03).

Ranking happens inside SQLite (`bm25()` / `ORDER BY rank`), so the schema keeps
the FTS5 mirrors as external-content tables over `chunks` and never duplicates
the body text.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path

DB_DIR = Path(".cq")
# v1: initial schema.
# v2: refs.target_symbol_id removed — cq does not resolve call targets to symbol
#     ids, so the column was永久に NULL のままだった。
# v3: files.parser now names the parser that actually ran (regex vs ast) and
#     chunks.symbol_id holds a real symbols.symbol_id instead of a qualname.
SCHEMA_VERSION = 3

_PROFILE_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

# 統計の集計対象。CLI と GUI の両方がここを参照する（FR-MAINT-07）。
STATS_TABLES: tuple[str, ...] = (
    "files", "symbols", "chunks", "refs", "imports", "traces",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
  path       TEXT PRIMARY KEY,
  lang       TEXT NOT NULL,
  sha1       TEXT NOT NULL,
  mtime      REAL NOT NULL,
  size_bytes INTEGER NOT NULL,
  parser     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS symbols (
  symbol_id  TEXT PRIMARY KEY,
  path       TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
  qualname   TEXT NOT NULL,
  name       TEXT NOT NULL,
  kind       TEXT NOT NULL,
  start_line INTEGER NOT NULL,
  end_line   INTEGER NOT NULL,
  signature  TEXT NOT NULL,
  parent     TEXT,
  doc_head   TEXT,
  decorators TEXT,
  is_test    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_path ON symbols(path);
CREATE TABLE IF NOT EXISTS chunks (
  chunk_id   TEXT PRIMARY KEY,
  path       TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
  symbol_id  TEXT,
  name       TEXT NOT NULL DEFAULT '',
  signature  TEXT NOT NULL DEFAULT '',
  ident_text TEXT NOT NULL DEFAULT '',
  start_line INTEGER NOT NULL,
  end_line   INTEGER NOT NULL,
  token_est  INTEGER NOT NULL DEFAULT 0,
  text       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);
CREATE TABLE IF NOT EXISTS refs (
  path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
  line INTEGER NOT NULL,
  name TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_refs_name ON refs(name);
CREATE TABLE IF NOT EXISTS imports (
  path   TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
  line   INTEGER NOT NULL,
  module TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_imports_module ON imports(module);
CREATE TABLE IF NOT EXISTS traces (
  path     TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
  line     INTEGER NOT NULL,
  trace_id TEXT NOT NULL,
  doc_path TEXT,
  anchor   TEXT
);
CREATE INDEX IF NOT EXISTS idx_traces_id ON traces(trace_id);
"""

# 部分一致・GLOB/LIKE 専用。detail=none で索引サイズを削減する（SQLite FTS5 §4.3.4 / §4.6）。
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_tri USING fts5(
  text, content='chunks', content_rowid='rowid',
  tokenize='trigram', detail=none
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  name, signature, ident_text, text,
  content='chunks', content_rowid='rowid',
  tokenize="unicode61 tokenchars '_$'", detail=column
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_tri(rowid, text) VALUES (new.rowid, new.text);
  INSERT INTO chunks_fts(rowid, name, signature, ident_text, text)
    VALUES (new.rowid, new.name, new.signature, new.ident_text, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_tri(chunks_tri, rowid, text) VALUES('delete', old.rowid, old.text);
  INSERT INTO chunks_fts(chunks_fts, rowid, name, signature, ident_text, text)
    VALUES('delete', old.rowid, old.name, old.signature, old.ident_text, old.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
  INSERT INTO chunks_tri(chunks_tri, rowid, text) VALUES('delete', old.rowid, old.text);
  INSERT INTO chunks_fts(chunks_fts, rowid, name, signature, ident_text, text)
    VALUES('delete', old.rowid, old.name, old.signature, old.ident_text, old.text);
  INSERT INTO chunks_tri(rowid, text) VALUES (new.rowid, new.text);
  INSERT INTO chunks_fts(rowid, name, signature, ident_text, text)
    VALUES (new.rowid, new.name, new.signature, new.ident_text, new.text);
END;
"""


class StoreError(RuntimeError):
    """Raised when the index cannot be opened or the profile name is unusable."""


class SchemaVersionError(StoreError):
    """Raised when an existing index was built by a different schema version."""


def db_path_for(profile: str) -> Path:
    """Return ``.cq/index-<profile>.sqlite`` for a validated profile name."""
    if not isinstance(profile, str) or not _PROFILE_RE.fullmatch(profile):
        raise StoreError(f"invalid profile name: {profile!r}")
    return DB_DIR / f"index-{profile}.sqlite"


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    return int(row[0]) if row else 0


def _language_breakdown(conn: sqlite3.Connection) -> dict[str, dict]:
    """Per-language counts (FR-CQ-15).

    `by_parser` alone cannot show where fidelity dropped, because one parser
    name is shared by several languages (C# and JavaScript are both `regex`).
    The language is read from the index as-is and never re-derived here.
    """
    parsers: dict[str, dict[str, int]] = {}
    for lang, parser, count in conn.execute(
        "SELECT lang, parser, count(*) FROM files"
        " GROUP BY lang, parser ORDER BY lang, parser"
    ):
        parsers.setdefault(lang, {})[parser] = count
    totals = {
        table: dict(conn.execute(
            f"SELECT f.lang, count(*) FROM {table} t"
            f" JOIN files f ON f.path = t.path GROUP BY f.lang"
        ))
        for table in ("symbols", "chunks")
    }
    return {
        lang: {
            "files": sum(by_parser.values()),
            # A language can index files yet expose no symbol (batch has no functions).
            "symbols": totals["symbols"].get(lang, 0),
            "chunks": totals["chunks"].get(lang, 0),
            "by_parser": by_parser,
        }
        for lang, by_parser in parsers.items()
    }


def index_stats(db_path: Path | str) -> dict[str, object]:
    """Row counts per table, parser fidelity split and schema version.

    Single implementation shared by the CLI (`cq stats`) and the GUI settings
    panel (FR-MAINT-07). A missing index raises via ``open_store(create=False)``
    rather than reporting zeros, so an absent index is never mistaken for an
    empty one.
    """
    with closing(open_store(db_path, create=False)) as conn:
        counts: dict[str, object] = {
            name: conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            for name in STATS_TABLES
        }
        counts["by_parser"] = dict(
            conn.execute("SELECT parser, count(*) FROM files GROUP BY parser")
        )
        counts["by_lang"] = _language_breakdown(conn)
        counts["schema_version"] = schema_version(conn)
    counts["db"] = str(db_path)
    return counts


def open_store(path: Path | str, *, create: bool = True) -> sqlite3.Connection:
    target = Path(path)
    if not target.exists():
        if not create:
            raise StoreError(f"cq index not found: {target}. Run `python -m cq index` first.")
        target.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(target)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        existing = _existing_version(conn)
        if existing is None:
            _create(conn)
        elif existing != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"cq index {target} was built with schema v{existing}, "
                f"expected v{SCHEMA_VERSION}. Delete it and rebuild "
                f"(`python -m cq index --rebuild`)."
            )
        return conn
    except Exception:
        conn.close()
        raise


def _existing_version(conn: sqlite3.Connection) -> int | None:
    with closing(conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
    )) as cursor:
        if cursor.fetchone() is None:
            return None
    return schema_version(conn)


def _create(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    try:
        conn.executescript(FTS_SCHEMA)
    except sqlite3.OperationalError as exc:
        raise StoreError(
            "this SQLite build lacks FTS5; cq requires FTS5 with the trigram tokenizer"
        ) from exc
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
