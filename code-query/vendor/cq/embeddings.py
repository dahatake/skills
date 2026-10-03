"""Local embedding provider for the semantic route (FR-CQ-17).

Deliberately independent from ``mdq.embeddings``: ``cq`` must not import ``mdq``
(FR-CQ-01 / FR-KIT-05), so the ~100 lines of provider plumbing are duplicated
rather than shared. A third shared package would force both distribution kits to
vendor it.

The backend (``fastembed``) is imported lazily so that plain searches never pay
for it (NFR-CQ-01). Without the optional dependency, callers get
:class:`EmbeddingsUnavailable` and degrade to the lexical routes.
"""

from __future__ import annotations

import functools
import os
import struct

# 前回の意味検索評価（NO-GO）と同じモデル。実測値（DL 240.5 MiB / dim 384 /
# 実チャンク 35.3 t/s / cq hve +401.8 秒）をそのまま比較基準に使えるようにする。
# fastembed は完全な HuggingFace 名を要求する（短名だと ValueError）。
DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


class EmbeddingsUnavailable(RuntimeError):
    """Raised when the optional embedding backend cannot be loaded."""


def _load_backend(model: str):
    """Return a ``fastembed.TextEmbedding``-shaped object, or ``None``."""
    try:
        from fastembed import TextEmbedding
    except Exception:  # noqa: BLE001 - any import failure means "not available"
        return None
    try:
        return TextEmbedding(model_name=model)
    except Exception as exc:  # noqa: BLE001
        raise EmbeddingsUnavailable(f"cannot load embedding model {model!r}: {exc}") from exc


class EmbeddingProvider:
    """Batch text encoder returning L2-normalised float32 rows."""

    def __init__(self, model: str, backend) -> None:
        self.model = model
        self._backend = backend

    def embed(self, texts):
        import numpy as np

        rows = list(self._backend.embed(list(texts)))
        if not rows:
            return np.zeros((0, 0), dtype="float32")
        matrix = np.asarray(rows, dtype="float32")
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        # 空文字列は零ベクトルになりうる。0 除算を避けて零のまま返す。
        return matrix / np.where(norms == 0.0, 1.0, norms)


@functools.lru_cache(maxsize=4)
def get_provider(model: str | None = None) -> EmbeddingProvider:
    """Return a cached provider. Indexing calls this once per chunk batch."""
    name = model or os.environ.get("CQ_EMBED_MODEL") or DEFAULT_MODEL
    try:
        import numpy  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        raise EmbeddingsUnavailable("the semantic route needs numpy") from exc
    backend = _load_backend(name)
    if backend is None:
        raise EmbeddingsUnavailable(
            "the semantic route needs the optional 'fastembed' dependency"
        )
    return EmbeddingProvider(name, backend)


def to_bytes(vector) -> bytes:
    return struct.pack(f"<{len(vector)}f", *(float(v) for v in vector))


def from_bytes(blob: bytes):
    import numpy as np

    return np.frombuffer(blob, dtype="<f4")
