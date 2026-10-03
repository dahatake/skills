"""Vector store for the semantic route, kept beside the main index (FR-CQ-17).

The vectors live in their own SQLite file rather than in ``chunks``: adding a
column would bump ``store.SCHEMA_VERSION``, and ``store.open_store`` rejects a
mismatched database fail-closed. That would force every user to rebuild the
whole index (106 s for the hve profile) for a feature that is off by default.

The cost of that separation is drift. ``chunks.chunk_id`` is derived from the
path and the chunk index, not from the content, so a stale row keeps its id
after an edit. Each row therefore carries the SHA-1 of the *file* it came from
(``files.sha1``), which the query side compares against the live index -- 830
rows for the hve profile, cheap enough to check on every search. Rows whose file
changed are treated as absent, degrading the semantic route to "fewer
candidates" instead of "wrong answers".
"""

from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence

from cq import embeddings

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunk_vectors (
  chunk_id TEXT PRIMARY KEY,
  path     TEXT NOT NULL,
  sha1     TEXT NOT NULL,
  vector   BLOB NOT NULL
);
"""


def db_path_for(profile: str) -> Path:
    return Path(".cq") / f"vectors-{profile}.sqlite"


@contextmanager
def open_store(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def replace(
    conn: sqlite3.Connection,
    model: str,
    rows: Iterable[tuple[str, str, str, Sequence[float]]],
) -> int:
    """Rewrite the whole store. Partial updates would need chunk-level deletes."""
    conn.execute("DELETE FROM chunk_vectors")
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('model', ?)", (model,)
    )
    payload = [
        (chunk_id, path, sha1, embeddings.to_bytes(vector))
        for chunk_id, path, sha1, vector in rows
    ]
    conn.executemany(
        "INSERT INTO chunk_vectors(chunk_id, path, sha1, vector) VALUES (?, ?, ?, ?)",
        payload,
    )
    return len(payload)


def model_of(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key='model'").fetchone()
    return row["value"] if row else None


def read_all(path: Path, model: str, fresh: Mapping[str, str]) -> dict[str, object]:
    """Return vectors whose file is unchanged, or ``{}`` if the store is unusable."""
    if not Path(path).is_file():
        return {}
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        try:
            if model_of(conn) != model:
                return {}
            rows = conn.execute(
                "SELECT chunk_id, path, sha1, vector FROM chunk_vectors"
            ).fetchall()
        except sqlite3.DatabaseError:
            return {}
    return {
        row["chunk_id"]: embeddings.from_bytes(row["vector"])
        for row in rows
        if fresh.get(row["path"]) == row["sha1"]
    }


def rank(query_vector, pool: dict[str, object], top_k: int) -> list[tuple[str, float]]:
    """Cosine ranking over already L2-normalised rows, so a dot product suffices."""
    if not pool:
        return []
    import numpy as np

    keys = sorted(pool)
    matrix = np.asarray([pool[key] for key in keys], dtype="float32")
    scores = matrix @ np.asarray(query_vector, dtype="float32")
    order = sorted(range(len(keys)), key=lambda i: (-float(scores[i]), keys[i]))
    return [(keys[i], float(scores[i])) for i in order[:top_k]]
