"""Embedding provider abstraction for the `semantic_paragraph` strategy.

Design (per work/semantic-paragraph/plan.md):
- Q1=A: default provider = **fastembed** (ONNX, CPU-only).
- Q2=C: default model    = **intfloat/multilingual-e5-large** (multilingual, MIT, ~2.2GB first DL).
- Q8=A: every default is overridable via env var or CLI flag.

Note: switched from BAAI/bge-m3 because fastembed 0.8.0+ removed bge-m3 from
TextEmbedding.list_supported_models(). multilingual-e5-large is MIT-licensed
(commercial use OK) and well-supported for Japanese.

Public API:
- :class:`EmbeddingProvider` ABC with :meth:`embed(texts: list[str]) -> np.ndarray`
  returning a `(n_texts, dim)` float32 numpy array.
- :func:`get_provider(name=None, model=None) -> EmbeddingProvider`
  factory. ``name`` defaults to the value of ``MDQ_EMBED_PROVIDER`` env var
  or ``"fastembed"``. ``model`` defaults to ``MDQ_EMBED_MODEL`` env var
  or ``"intfloat/multilingual-e5-large"``.
- :func:`cosine_distances(vecs: np.ndarray) -> np.ndarray`
  helper returning the `(n-1,)` array of distances between consecutive rows.

When ``fastembed`` (or ``numpy``) is not installed, :func:`get_provider`
raises :class:`EmbeddingsUnavailable`. The strategy layer catches this and
falls back to ``heading_recursive`` with a clear stderr warning, per Q1=A
and the risk-mitigation section of the plan.

The provider does *not* perform on-disk caching of vectors here; that is
deferred to the chunker level (`.mdq/embed-cache-<sha1>`).
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Iterable, Sequence

# --- Exceptions ------------------------------------------------------------


class EmbeddingsUnavailable(RuntimeError):
    """Raised when the requested embedding provider cannot be loaded.

    The CLI/strategy layer should catch this and either:
      1. Fall back to ``heading_recursive`` (default behaviour), or
      2. Surface the error to the user with `--strict-embeddings`.
    """


# --- ABC -------------------------------------------------------------------


class EmbeddingProvider(ABC):
    """Abstract base class for batch text embedding."""

    name: str = "abstract"
    model: str = ""
    dim: int = 0

    @abstractmethod
    def embed(self, texts: Sequence[str]):  # -> np.ndarray
        """Embed ``texts`` and return a ``(len(texts), self.dim)`` array."""


# --- Concrete: fastembed ---------------------------------------------------


class FastEmbedProvider(EmbeddingProvider):
    """fastembed (ONNX) provider. Default per Q1=A / Q2=C."""

    name = "fastembed"

    def __init__(self, model: str = "intfloat/multilingual-e5-large"):
        try:
            from fastembed import TextEmbedding  # type: ignore
            import numpy as np  # noqa: F401  # imported for type contract
        except ImportError as e:
            raise EmbeddingsUnavailable(
                f"fastembed/numpy not installed: {e}. "
                "Install with: pip install -e .[semantic]"
            ) from e
        try:
            self._model = TextEmbedding(model_name=model)
        except Exception as e:  # noqa: BLE001 -- any init error is fatal here
            raise EmbeddingsUnavailable(
                f"failed to load fastembed model {model!r}: {e}"
            ) from e
        self.model = model
        # Probe dim with a 1-token query so we don't pay for a real batch.
        try:
            import numpy as _np
            sample = next(iter(self._model.embed(["x"])))
            self.dim = int(_np.asarray(sample).shape[-1])
        except Exception:
            self.dim = 0

    def embed(self, texts: Sequence[str]):
        import numpy as np

        if not texts:
            return np.zeros((0, self.dim or 1), dtype=np.float32)
        out = list(self._model.embed(list(texts)))
        arr = np.asarray(out, dtype=np.float32)
        if arr.ndim == 1:
            arr = arr.reshape(1, -1)
        return arr


# --- Concrete: null (test-only) -------------------------------------------


class NullProvider(EmbeddingProvider):
    """Deterministic hash-based provider for tests (no network, no install).

    Maps each text to a fixed-dim float32 vector derived from md5 hashing.
    The vectors are L2-normalised and reproducible across runs. NOT for
    production retrieval; only for unit-testing the chunker logic.
    """

    name = "null"

    def __init__(self, model: str = "null-md5-32", dim: int = 32):
        self.model = model
        self.dim = dim

    def embed(self, texts: Sequence[str]):
        import hashlib

        import numpy as np

        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            h = hashlib.md5(t.encode("utf-8")).digest()
            # Repeat the 16-byte digest until we have `dim` bytes.
            buf = (h * ((self.dim // 16) + 1))[: self.dim]
            v = np.frombuffer(buf, dtype=np.uint8).astype(np.float32)
            v = v - 127.5  # centre around 0
            norm = float(np.linalg.norm(v))
            if norm > 0:
                v = v / norm
            out[i] = v
        return out


# --- Factory ---------------------------------------------------------------

_REGISTRY = {
    "fastembed": FastEmbedProvider,
    "null": NullProvider,
}

_PROVIDER_CACHE: dict[tuple[str, str | None], EmbeddingProvider] = {}


def clear_provider_cache() -> None:
    """Drop every cached provider. Used by tests and after settings changes."""
    _PROVIDER_CACHE.clear()


def get_provider(name: str | None = None, model: str | None = None) -> EmbeddingProvider:
    """Return an :class:`EmbeddingProvider` instance.

    Resolution order:
      1. Explicit ``name``/``model`` arguments.
      2. Environment variables ``MDQ_EMBED_PROVIDER`` / ``MDQ_EMBED_MODEL``.
      3. Defaults: provider=``fastembed``, model=``intfloat/multilingual-e5-large``.

    Instances are cached per ``(provider, model)``. The semantic chunker asks
    for a provider once per file, so building the ONNX session every time was
    paid N times per index build. Measured end to end on this repository
    (162 files, paraphrase-multilingual-MiniLM-L12-v2): 467 s before, 385.5 s
    after. Call :func:`clear_provider_cache` to force a rebuild.
    """
    n = (name or os.environ.get("MDQ_EMBED_PROVIDER") or "fastembed").lower()
    m = model or os.environ.get("MDQ_EMBED_MODEL") or None
    if n not in _REGISTRY:
        raise EmbeddingsUnavailable(
            f"unknown embedding provider: {n!r}. "
            f"available: {sorted(_REGISTRY)}"
        )
    key = (n, m)
    cached = _PROVIDER_CACHE.get(key)
    if cached is not None:
        return cached
    cls = _REGISTRY[n]
    # A failed construction must not be cached: the exception propagates
    # before the assignment below.
    provider = cls() if m is None else cls(model=m)
    _PROVIDER_CACHE[key] = provider
    return provider


# --- Math helpers ----------------------------------------------------------


def cosine_distances(vecs):  # vecs: np.ndarray (n, dim)
    """Return the (n-1,) array of cosine distances between consecutive rows.

    Distance = 1 - cosine_similarity. Rows that are all-zero produce a
    distance of 1.0 (treated as maximally dissimilar).
    """
    import numpy as np

    arr = np.asarray(vecs, dtype=np.float32)
    if arr.ndim != 2 or arr.shape[0] < 2:
        return np.zeros((0,), dtype=np.float32)
    a = arr[:-1]
    b = arr[1:]
    num = np.sum(a * b, axis=1)
    den = np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1)
    sim = np.where(den > 0, num / np.maximum(den, 1e-12), 0.0)
    return (1.0 - sim).astype(np.float32)


__all__ = [
    "EmbeddingProvider",
    "EmbeddingsUnavailable",
    "FastEmbedProvider",
    "NullProvider",
    "get_provider",
    "clear_provider_cache",
    "cosine_distances",
]
