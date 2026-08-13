"""SQLite-backed persistent store for the Markdown index.

Schema is intentionally small. BM25 ranking is computed at query time over
the chunks loaded from this store (small/medium corpora).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

DEFAULT_DB_PATH = Path(".mdq") / "index.sqlite"

# Per (lang, strategy) DB layout. Legacy single-file DEFAULT_DB_PATH is kept
# for backward compatibility with callers that pass an explicit path.
_DB_DIR = Path(".mdq")


def db_path_for(lang: str = "ja-jp", strategy: str = "heading") -> Path:
    """Return ``.mdq/index-<lang>-<strategy>.sqlite``.

    The returned path is relative; callers should resolve it under the
    repository root before opening. ``lang`` and ``strategy`` are validated
    against the known allow-lists to prevent path traversal via
    user-editable settings files; unknown values fall back to defaults.
    """
    # Local import to avoid module-load cycles.
    from . import tokenize as _tok
    from . import strategies as _strat

    safe_lang = _tok.normalize(lang)
    safe_strategy = _strat.normalize(strategy)
    return _DB_DIR / f"index-{safe_lang}-{safe_strategy}.sqlite"

# Schema version - bump whenever the migration code adds/changes columns
# or changes chunk_id derivation (forcing a rebuild).
# v1: introduced part_index / part_total columns.
# v2: chunk_id derivation changed to use occurrence_index instead of
#     start_line, making IDs stable against line shifts. v1 chunk rows are
#     dropped and rebuilt from source on first open.
# v3: optional FTS5 mirror table `chunks_fts` + sync triggers. Builds an
#     empty FTS index initially; rebuild is requested on upgrade. Falls back
#     silently when the SQLite build lacks FTS5.
# v4: parent_chunk_id column added (nullable) referencing the chunk_id of
#     the nearest ancestor heading chunk. Populated during indexing; allows
#     fast O(1) parent lookup for --with-parent expansion. NULL for chunks
#     without an ancestor (top-level heading, preface, fixed_window rows).
# v5: text_raw + chunk_embedding columns added (both nullable) for the
#     `semantic_paragraph` chunking strategy.
#       - text_raw: original chunk body before contextualizer template
#         expansion (Q11=B). NULL means `text` is already the raw body.
#       - chunk_embedding: float32 BLOB produced by late-chunking (Q9=B).
#         NULL means the row was not indexed with --late-chunking.
#     Both columns are pure ADD COLUMN migrations; no data loss.
# v6: summary column added (nullable) for the `pageindex` chunking
#     strategy. NULL for chunks produced by other strategies; populated
#     with a deterministic head/first_paragraph extract by
#     :mod:`mdq.strategies_pageindex`. ADD COLUMN migration; no data loss.
# v7: the `trigram` FTS5 mirror is created with detail=none to cut index
#     size. Positional data is dropped, so phrase queries are unavailable
#     and :mod:`mdq.search` decomposes terms into ANDed trigrams instead.
#     Existing mirrors are dropped and rebuilt on upgrade.
SCHEMA_VERSION = 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  path        TEXT PRIMARY KEY,
  sha1        TEXT NOT NULL,
  mtime       REAL NOT NULL,
  size_bytes  INTEGER NOT NULL,
  frontmatter TEXT
);
CREATE TABLE IF NOT EXISTS chunks (
  chunk_id        TEXT PRIMARY KEY,
  path            TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
  heading_path    TEXT NOT NULL,
  level           INTEGER NOT NULL,
  start_line      INTEGER NOT NULL,
  end_line        INTEGER NOT NULL,
  token_est       INTEGER NOT NULL,
  text            TEXT NOT NULL,
  tags            TEXT,
  part_index      INTEGER NOT NULL DEFAULT 0,
  part_total      INTEGER NOT NULL DEFAULT 1,
  parent_chunk_id TEXT,
  text_raw        TEXT,
  chunk_embedding BLOB,
  summary         TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);
"""

# Separate from SCHEMA because legacy DBs (v3) lack the parent_chunk_id
# column at the time SCHEMA is executed; the index is created lazily in
# _migrate() after ALTER TABLE ADD COLUMN has run.
_PARENT_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_chunks_parent "
    "ON chunks(parent_chunk_id)"
)


def has_fts5(conn: sqlite3.Connection) -> bool:
    """Return True if this SQLite build supports FTS5."""
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x)"
        )
        conn.execute("DROP TABLE IF EXISTS _fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def _fts_table_args(tokenizer: str) -> str:
    """Return the argument list of the ``chunks_fts`` virtual table.

    ``trigram`` mirrors use ``detail=none``: without positional data the
    index is far smaller (SQLite FTS5 §4.3.4 / §4.6). The trade-off is that
    phrase queries raise, so :mod:`mdq.search` ANDs individual trigrams.
    """
    detail = ", detail=none" if tokenizer == "trigram" else ""
    return (
        f"text, content='chunks', content_rowid='rowid', "
        f"tokenize='{tokenizer}'{detail}"
    )


def _fts_schema(tokenizer: str = "unicode61") -> str:
    """Return the FTS5 mirror schema using the requested tokenizer.

    The tokenizer is interpolated rather than parameterised because SQLite
    does not allow bind parameters in DDL. Only known-safe values from
    :mod:`mdq.tokenize` should be passed in.
    """
    return f"""
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  {_fts_table_args(tokenizer)}
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES('delete', old.rowid, old.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES('delete', old.rowid, old.text);
  INSERT INTO chunks_fts(rowid, text) VALUES (new.rowid, new.text);
END;
"""


# Backward-compat alias for any external import.
_FTS_SCHEMA = _fts_schema("unicode61")


def _migrate(conn: sqlite3.Connection, fts_tokenizer: str = "unicode61") -> None:
    """Apply lightweight ADD COLUMN migrations for legacy DBs.

    Idempotent: safe to call on every open_store(). We rely on PRAGMA
    table_info() rather than user_version alone so that DBs created before
    user_version was introduced are still upgraded.
    """
    cur = conn.execute("PRAGMA table_info(chunks)")
    cols = {row[1] for row in cur}
    if "chunks" and cols:
        if "part_index" not in cols:
            conn.execute(
                "ALTER TABLE chunks ADD COLUMN part_index INTEGER NOT NULL DEFAULT 0"
            )
        if "part_total" not in cols:
            conn.execute(
                "ALTER TABLE chunks ADD COLUMN part_total INTEGER NOT NULL DEFAULT 1"
            )
        if "parent_chunk_id" not in cols:
            conn.execute(
                "ALTER TABLE chunks ADD COLUMN parent_chunk_id TEXT"
            )
        # v4 -> v5: ADD COLUMN text_raw + chunk_embedding (both nullable).
        if "text_raw" not in cols:
            conn.execute("ALTER TABLE chunks ADD COLUMN text_raw TEXT")
        if "chunk_embedding" not in cols:
            conn.execute(
                "ALTER TABLE chunks ADD COLUMN chunk_embedding BLOB"
            )
        # v5 -> v6: ADD COLUMN summary (nullable, pageindex strategy).
        if "summary" not in cols:
            conn.execute("ALTER TABLE chunks ADD COLUMN summary TEXT")
    # Always ensure the parent index exists once the column is guaranteed.
    conn.execute(_PARENT_INDEX_DDL)
    # v1 -> v2: chunk_id derivation changed. Drop chunks and clear file SHA-1
    # so the next index run rebuilds everything with stable IDs. files rows
    # are preserved (frontmatter, mtime) but sha1 is wiped to force re-scan.
    prev_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if prev_version < 2 and cols:
        conn.execute("DELETE FROM chunks")
        conn.execute("UPDATE files SET sha1 = ''")
    # v3 -> v4: parent_chunk_id column added. Existing rows keep NULL until
    # the next indexing pass populates them; no destructive migration is
    # required because callers fall back to heading_path resolution when the
    # column is NULL.
    if prev_version < 4 and cols and "parent_chunk_id" not in cols:
        # Column was just added above; nothing else to do.
        pass
    # v* -> v3: install FTS5 mirror (best effort; SQLite without FTS5 simply
    # continues to use the BM25 fallback path).
    if has_fts5(conn):
        # If the existing FTS5 mirror was created with different arguments
        # (tokenizer or detail level), we need to drop and recreate it.
        # Detect by inspecting the stored CREATE TABLE statement.
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
        ).fetchone()
        existing_sql = (row[0] if row else "") or ""
        wanted_marker = _fts_table_args(fts_tokenizer)
        if row and wanted_marker not in existing_sql:
            # Tokenizer mismatch detected — recreating the FTS5 mirror is
            # expensive on large DBs. Emit a logger warning so users invoking
            # `open_store(custom.sqlite, lang=...)` with a previously-other
            # language are not silently surprised by a long migration.
            import logging as _lg
            _lg.getLogger(__name__).warning(
                "FTS5 schema mismatch in %s (existing has %r, want %r); "
                "rebuilding chunks_fts. This can take a while on large DBs.",
                getattr(conn, "filename", "<unknown>"),
                existing_sql,
                wanted_marker,
            )
            for stmt in (
                "DROP TRIGGER IF EXISTS chunks_ai",
                "DROP TRIGGER IF EXISTS chunks_ad",
                "DROP TRIGGER IF EXISTS chunks_au",
                "DROP TABLE IF EXISTS chunks_fts",
            ):
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError:
                    pass
            prev_version = 0  # force rebuild below
        conn.executescript(_fts_schema(fts_tokenizer))
        if prev_version < 3:
            try:
                conn.execute(
                    "INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')"
                )
            except sqlite3.OperationalError:
                pass
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def open_store(
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    lang: str = "ja-jp",
) -> sqlite3.Connection:
    """Open (or create) the SQLite store.

    ``lang`` selects the FTS5 tokenizer (``ja-jp`` -> ``trigram`` when
    available, otherwise ``unicode61``; ``en-us`` -> ``unicode61``).
    Existing DBs created with a different tokenizer are migrated in-place by
    dropping/recreating the FTS5 mirror.
    """
    from . import tokenize as _tok  # local import to avoid cycles

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(SCHEMA)
    fts_tok = _tok.resolved_fts5_tokenizer(conn, _tok.normalize(lang))
    _migrate(conn, fts_tokenizer=fts_tok)
    conn.commit()
    return conn


def upsert_file(conn: sqlite3.Connection, path: str, sha1: str, mtime: float,
                size_bytes: int, frontmatter_json: str | None) -> None:
    conn.execute(
        "INSERT INTO files(path, sha1, mtime, size_bytes, frontmatter) VALUES(?,?,?,?,?) "
        "ON CONFLICT(path) DO UPDATE SET sha1=excluded.sha1, mtime=excluded.mtime, "
        "size_bytes=excluded.size_bytes, frontmatter=excluded.frontmatter",
        (path, sha1, mtime, size_bytes, frontmatter_json),
    )


def delete_chunks_for(conn: sqlite3.Connection, path: str) -> None:
    conn.execute("DELETE FROM chunks WHERE path = ?", (path,))


def insert_chunks(conn: sqlite3.Connection, rows: Iterable[tuple]) -> None:
    """Insert chunk rows.

    Accepts 9-, 11-, 12-, 14- or 15-tuples (all back-compat):
      - 9-tuple  (legacy): through tags. part_index/part_total/parent default.
      - 11-tuple         : adds (part_index, part_total). parent NULL.
      - 12-tuple         : adds parent_chunk_id at the end.
      - 14-tuple (v5)    : adds (text_raw, chunk_embedding) at the end.
      - 15-tuple (v6)    : adds summary at the end.
    """
    materialised = []
    for r in rows:
        if len(r) == 9:
            materialised.append((*r, 0, 1, None, None, None, None))
        elif len(r) == 11:
            materialised.append((*r, None, None, None, None))
        elif len(r) == 12:
            materialised.append((*r, None, None, None))
        elif len(r) == 14:
            materialised.append((*r, None))
        else:
            materialised.append(tuple(r))
    conn.executemany(
        "INSERT OR REPLACE INTO chunks(chunk_id, path, heading_path, level, "
        "start_line, end_line, token_est, text, tags, part_index, part_total, "
        "parent_chunk_id, text_raw, chunk_embedding, summary) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        materialised,
    )


def get_file_meta(conn: sqlite3.Connection, path: str) -> tuple[str, float] | None:
    cur = conn.execute("SELECT sha1, mtime FROM files WHERE path = ?", (path,))
    row = cur.fetchone()
    return (row[0], row[1]) if row else None


def all_chunks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return list(conn.execute(
        "SELECT chunk_id, path, heading_path, level, start_line, end_line, "
        "token_est, text, tags, part_index, part_total, parent_chunk_id, "
        "text_raw, chunk_embedding, summary "
        "FROM chunks"
    ))


def list_all_paths(conn: sqlite3.Connection) -> set[str]:
    """Return all file paths currently registered in the store."""
    return {row[0] for row in conn.execute("SELECT path FROM files")}


def delete_file(conn: sqlite3.Connection, path: str) -> int:
    """Delete a file row (chunks are removed via ON DELETE CASCADE).

    Returns the number of chunk rows removed.
    """
    n = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE path = ?", (path,)
    ).fetchone()[0]
    conn.execute("DELETE FROM files WHERE path = ?", (path,))
    return int(n)


def stats(conn: sqlite3.Connection) -> dict:
    f = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    c = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    return {"files": f, "chunks": c}
