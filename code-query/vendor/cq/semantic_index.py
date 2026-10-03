"""Build the vector side-index for the semantic route (FR-CQ-17).

What gets embedded matters more than the model. The 2026-08-04 evaluation
embedded ``name + signature + text[:512]`` -- raw code -- and both Japanese
``natural`` golden queries stayed out of range. In this repository 5,432 of the
6,273 documented symbols in the hve profile (86.6%) have a Japanese
``doc_head``, so the docstring is embedded instead of the body whenever there is
one, giving a Japanese-to-Japanese path. Chunks without a docstring keep the old
body-prefix behaviour so that profiles with almost no documentation (app: 10 of
1,559 symbols) still get vectors.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from cq import store, vectors

# 前回 PoC と同じ本文長。埋め込みテキストの違いだけを切り分けられるようにする。
BODY_CHARS = 512
BATCH = 256


def embedding_text(name: str, signature: str, doc_head: str | None, body: str) -> str:
    parts = [part for part in (name, signature) if part]
    parts.append(doc_head.strip() if doc_head and doc_head.strip() else body[:BODY_CHARS])
    return "\n".join(parts)


def _rows(conn):
    return conn.execute(
        "SELECT c.chunk_id, c.path, c.name, c.signature, c.text, s.doc_head, f.sha1 "
        "FROM chunks c "
        "JOIN files f ON f.path = c.path "
        "LEFT JOIN symbols s ON s.symbol_id = c.symbol_id "
        "ORDER BY c.chunk_id"
    ).fetchall()


def build(
    repo_root: Path,
    profile: str,
    provider,
    *,
    db_path: Path | None = None,
    vector_path: Path | None = None,
) -> int:
    """Embed every chunk of ``profile`` and rewrite its vector store."""
    index = Path(db_path) if db_path else repo_root / store.db_path_for(profile)
    target = Path(vector_path) if vector_path else repo_root / vectors.db_path_for(profile)

    with closing(store.open_store(index, create=False)) as conn:
        rows = _rows(conn)

    payload = []
    for start in range(0, len(rows), BATCH):
        batch = rows[start : start + BATCH]
        texts = [
            embedding_text(row["name"], row["signature"], row["doc_head"], row["text"])
            for row in batch
        ]
        encoded = provider.embed(texts)
        for row, vector in zip(batch, encoded):
            payload.append((row["chunk_id"], row["path"], row["sha1"], vector))

    with vectors.open_store(target) as conn:
        return vectors.replace(conn, provider.model, payload)
