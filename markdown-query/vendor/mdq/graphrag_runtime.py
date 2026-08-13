"""GraphRAG runtime helpers for the ``graphrag`` strategy.

This module provides LightRAG-compatible LLM completion and embedding
callables backed by Ollama (loopback only) or deterministic mocks for
tests. It does **not** import ``lightrag`` itself; the strategy layer
(:mod:`mdq.strategies_graphrag`, T3.1) is responsible for lazy-importing
LightRAG and wiring these callables in.

Design constraints (from ``plan-graphrag.md`` §5):

- **R2**: Ollama ``base_url`` must be loopback-only by default. Set
  ``allow_remote=True`` (CLI flag ``--graphrag-allow-remote-ollama``) to
  bypass.
- **R4**: never import ``lightrag.llm.*`` (it triggers ``pipmaster`` auto
  installs of ``ollama``/``openai``/etc.). This module talks to Ollama
  via the standard-library ``urllib`` HTTP client.
- **R6**: the completion callable signature must be LightRAG
  ``llm_model_func`` compatible:
  ``async def(prompt, system_prompt=None, history_messages=None, **kwargs) -> str``.

Public API:

- :class:`GraphRAGRuntimeUnavailable` -- raised when a backend cannot be
  initialised (e.g. ``numpy`` not installed for embedding output).
- :func:`validate_loopback_url` -- pure helper implementing R2.
- :func:`make_ollama_completion` / :func:`make_ollama_embedding` -- async
  callable factories for the Ollama HTTP API.
- :func:`make_mock_completion` / :func:`make_mock_embedding` -- deterministic
  mocks for unit tests (no network).
- :func:`get_completion_func` / :func:`get_embedding_func` -- top-level
  factories selecting between ``"ollama"`` and ``"mock"``.

This module is intentionally minimal. It does not introduce an ABC layer
or strategy pattern; the two backends share a callable contract enforced
by tests, which is sufficient for the current scope (no third backend
planned).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import struct
import urllib.error
import urllib.parse
import urllib.request
import warnings
from typing import Any, Awaitable, Callable, List, Sequence
from urllib.parse import urlsplit

# --- Exceptions ------------------------------------------------------------


class GraphRAGRuntimeUnavailable(RuntimeError):
    """Raised when a GraphRAG runtime backend cannot be initialised.

    Examples:
      - ``numpy`` is not installed (required for embedding output arrays).
      - Ollama backend was requested but ``base_url`` is non-loopback and
        ``allow_remote=False``.
    """


# --- R2: loopback validation ----------------------------------------------

# Hosts that resolve to the local machine. We compare against the parsed
# hostname (lowercased, brackets stripped) so that ``http://[::1]:11434``
# and ``http://localhost:11434`` are both accepted.
_LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})


def validate_loopback_url(url: str, allow_remote: bool = False) -> str:
    """Validate ``url`` is loopback (R2) and return the normalised value.

    Parameters
    ----------
    url:
        Base URL of the Ollama HTTP API (e.g. ``http://127.0.0.1:11434``).
    allow_remote:
        When ``True``, skip the loopback check. The CLI sets this only
        when ``--graphrag-allow-remote-ollama`` is passed.

    Raises
    ------
    ValueError
        If ``url`` has no scheme/host, or is non-loopback while
        ``allow_remote`` is ``False``.
    """
    if not url or not isinstance(url, str):
        raise ValueError(f"graphrag: base_url must be a non-empty string, got {url!r}")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise ValueError(
            f"graphrag: base_url must use http/https scheme, got {parts.scheme!r}"
        )
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError(f"graphrag: base_url has no host: {url!r}")
    if allow_remote:
        if host not in _LOOPBACK_HOSTNAMES:
            # R2 belt-and-braces: even when the caller enabled remote mode,
            # surface a RuntimeWarning so that misconfigured deployments are
            # visible in stderr/log capture. The CLI also prints a banner.
            warnings.warn(
                f"graphrag: remote Ollama endpoint enabled (host={host!r}); "
                "corpus text will be sent to a non-loopback LLM endpoint.",
                RuntimeWarning,
                stacklevel=2,
            )
        return url
    if host not in _LOOPBACK_HOSTNAMES:
        raise ValueError(
            f"graphrag: base_url {url!r} is non-loopback (host={host!r}); "
            "pass --graphrag-allow-remote-ollama to override. "
            "GraphRAG is local-only by default to avoid leaking corpus data "
            "to remote LLM endpoints."
        )
    return url


# --- HTTP helper (sync) ----------------------------------------------------


def _http_post_json(url: str, payload: dict, timeout: float) -> dict:
    """POST ``payload`` as JSON and return the parsed JSON response.

    Uses ``urllib`` only -- no ``requests``/``httpx`` dependency. Raises
    :class:`GraphRAGRuntimeUnavailable` on HTTP/URL errors so that callers
    can surface a uniform failure.
    """
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        raise GraphRAGRuntimeUnavailable(
            f"graphrag: Ollama HTTP {e.code} at {url}: {detail[:500]}"
        ) from e
    except urllib.error.URLError as e:
        raise GraphRAGRuntimeUnavailable(
            f"graphrag: Ollama connection failed at {url}: {e.reason}"
        ) from e
    except TimeoutError as e:
        raise GraphRAGRuntimeUnavailable(
            f"graphrag: Ollama request timed out at {url} after {timeout}s"
        ) from e
    except OSError as e:
        # Catches socket-level errors (ConnectionResetError, BrokenPipeError,
        # mid-read timeouts re-raised as OSError) that escape the URLError
        # wrapper after a successful connect.
        raise GraphRAGRuntimeUnavailable(
            f"graphrag: Ollama I/O error at {url}: {e}"
        ) from e
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise GraphRAGRuntimeUnavailable(
            f"graphrag: Ollama returned non-JSON at {url}: {e}"
        ) from e


# --- Ollama completion -----------------------------------------------------


def make_ollama_completion(
    base_url: str,
    model: str,
    *,
    timeout: float = 120.0,
    allow_remote: bool = False,
    num_predict: int | None = None,
) -> Callable[..., Awaitable[str]]:
    """Build an Ollama-backed completion callable (R6-compatible).

    Returns an ``async def`` accepting LightRAG's ``llm_model_func``
    keyword surface (``prompt, system_prompt, history_messages, **kwargs``)
    and returning the generated text as a single string. All
    LightRAG-internal kwargs (``hashing_kv``, ``keyword_extraction``,
    ``response_format``, ``stream``, ``timeout`` etc.) are accepted and
    ignored except where they map to Ollama options.

    Parameters
    ----------
    base_url:
        Ollama base URL. Validated by :func:`validate_loopback_url`.
    model:
        Ollama model name (e.g. ``"qwen2.5:7b"``).
    timeout:
        Per-request timeout in seconds.
    allow_remote:
        Forwarded to :func:`validate_loopback_url` (R2).
    num_predict:
        Optional Ollama ``num_predict`` (max tokens) override.
    """
    url = validate_loopback_url(base_url, allow_remote=allow_remote).rstrip("/")
    endpoint = f"{url}/api/generate"

    async def _complete(
        prompt: str,
        system_prompt: str | None = None,
        history_messages: list | None = None,
        **kwargs: Any,
    ) -> str:
        # Compose the final prompt. Ollama's /api/generate has a single
        # `prompt` field plus an optional `system`. History messages are
        # flattened into the prompt to keep this transport minimal and
        # deterministic (LightRAG itself handles longer-form chat state).
        composed_prompt = prompt
        if history_messages:
            parts: list[str] = []
            for msg in history_messages:
                if not isinstance(msg, dict):
                    continue
                role = str(msg.get("role", "user"))
                content = str(msg.get("content", ""))
                parts.append(f"{role}: {content}")
            if parts:
                composed_prompt = "\n".join(parts) + "\n" + prompt
        payload: dict[str, Any] = {
            "model": model,
            "prompt": composed_prompt,
            "stream": False,
        }
        if system_prompt:
            payload["system"] = system_prompt
        if num_predict is not None:
            payload.setdefault("options", {})["num_predict"] = int(num_predict)
        # LightRAG sets ``keyword_extraction=True`` (and may also pass
        # ``response_format={"type": "json_object"}``) when it wants strict
        # JSON output, e.g. inside extract_keywords. Forwarding these as
        # Ollama's ``format="json"`` matches LightRAG's bundled Ollama
        # provider so that downstream JSON parsing succeeds.
        if kwargs.get("keyword_extraction"):
            payload["format"] = "json"
        else:
            rf = kwargs.get("response_format")
            if isinstance(rf, str) and rf.lower() == "json":
                payload["format"] = "json"
            elif isinstance(rf, dict) and str(rf.get("type", "")).lower() in {
                "json",
                "json_object",
            }:
                payload["format"] = "json"
        data = await asyncio.to_thread(_http_post_json, endpoint, payload, timeout)
        return str(data.get("response", ""))

    return _complete


# --- Ollama embedding ------------------------------------------------------


def make_ollama_embedding(
    base_url: str,
    model: str,
    *,
    timeout: float = 60.0,
    allow_remote: bool = False,
) -> tuple[Callable[[Sequence[str]], Awaitable[Any]], int]:
    """Build an Ollama-backed embedding callable and probe its dimension.

    The first call probes ``model`` with a single token to determine the
    embedding dimension, which LightRAG's ``EmbeddingFunc`` requires
    upfront. The probe is synchronous and runs inside this factory so the
    returned callable is dimension-agnostic at call time.

    Returns
    -------
    (async_callable, dim):
        ``async_callable(texts) -> np.ndarray`` with shape ``(len(texts), dim)``.

    Raises
    ------
    GraphRAGRuntimeUnavailable
        If ``numpy`` is missing or the dimension probe fails.
    """
    try:
        import numpy as np  # type: ignore
    except ImportError as e:  # pragma: no cover - numpy is a hard dep of mdq
        raise GraphRAGRuntimeUnavailable(
            f"graphrag: numpy is required for embeddings: {e}"
        ) from e

    url = validate_loopback_url(base_url, allow_remote=allow_remote).rstrip("/")
    endpoint = f"{url}/api/embeddings"

    def _embed_one_sync(text: str) -> list[float]:
        data = _http_post_json(endpoint, {"model": model, "prompt": text}, timeout)
        vec = data.get("embedding")
        if not isinstance(vec, list) or not vec:
            raise GraphRAGRuntimeUnavailable(
                f"graphrag: Ollama embeddings returned no vector for model={model!r}"
            )
        return [float(x) for x in vec]

    probe = _embed_one_sync("x")
    dim = len(probe)
    if dim <= 0:
        raise GraphRAGRuntimeUnavailable(
            f"graphrag: Ollama returned zero-dimensional embedding for model={model!r}"
        )

    async def _embed(texts: Sequence[str]):
        if not texts:
            return np.zeros((0, dim), dtype=np.float32)
        # Ollama's /api/embeddings is single-text; batch sequentially to
        # keep the transport simple. Parallelism here would only help when
        # Ollama is serving multiple model instances, which is uncommon
        # for the local-only default deployment.
        vecs: list[list[float]] = []
        for t in texts:
            vec = await asyncio.to_thread(_embed_one_sync, str(t))
            vecs.append(vec)
        return np.asarray(vecs, dtype=np.float32)

    return _embed, dim


# --- Mock backends (deterministic, no network) -----------------------------


def make_mock_completion(
    prefix: str = "mock:",
) -> Callable[..., Awaitable[str]]:
    """Deterministic completion callable for unit tests.

    The returned string is ``f"{prefix}{md5(prompt + system_prompt)[:16]}"``
    so tests can assert reproducibility without invoking any model.
    """

    async def _complete(
        prompt: str,
        system_prompt: str | None = None,
        history_messages: list | None = None,
        **kwargs: Any,
    ) -> str:
        h = hashlib.md5()
        h.update((prompt or "").encode("utf-8"))
        if system_prompt:
            h.update(b"\x00")
            h.update(system_prompt.encode("utf-8"))
        return f"{prefix}{h.hexdigest()[:16]}"

    return _complete


def make_mock_embedding(
    dim: int = 64,
) -> tuple[Callable[[Sequence[str]], Awaitable[Any]], int]:
    """Deterministic embedding callable for unit tests (no network).

    Each text is hashed (md5) and the digest bytes are unpacked into
    ``dim`` float32 values normalised to L2 = 1. Reproducible across
    Python versions.
    """
    try:
        import numpy as np  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise GraphRAGRuntimeUnavailable(
            f"graphrag: numpy is required for mock embeddings: {e}"
        ) from e
    if dim <= 0:
        raise ValueError(f"graphrag: mock embedding dim must be > 0, got {dim}")

    def _hash_to_vec(text: str) -> List[float]:
        # Generate enough bytes by hashing the text repeatedly with a salt.
        out: List[float] = []
        i = 0
        while len(out) < dim:
            h = hashlib.md5(f"{i}:{text}".encode("utf-8")).digest()
            # md5 -> 16 bytes -> 4 floats (big-endian uint32 -> [0,1])
            for j in range(0, 16, 4):
                if len(out) >= dim:
                    break
                u = struct.unpack(">I", h[j : j + 4])[0]
                out.append(u / 2**32)
            i += 1
        return out

    async def _embed(texts: Sequence[str]):
        if not texts:
            return np.zeros((0, dim), dtype=np.float32)
        vecs = [_hash_to_vec(str(t)) for t in texts]
        arr = np.asarray(vecs, dtype=np.float32)
        # L2-normalise so cosine similarity is well-defined.
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return arr / norms

    return _embed, dim


# --- Top-level factories ---------------------------------------------------


def get_completion_func(
    provider: str = "ollama",
    *,
    base_url: str = "http://127.0.0.1:11434",
    model: str = "qwen2.5:7b",
    timeout: float = 120.0,
    allow_remote: bool = False,
    num_predict: int | None = None,
) -> Callable[..., Awaitable[str]]:
    """Return a LightRAG-compatible completion callable.

    ``provider`` is one of ``"ollama"`` or ``"mock"``. Unknown providers
    raise :class:`ValueError`.
    """
    if provider == "ollama":
        return make_ollama_completion(
            base_url,
            model,
            timeout=timeout,
            allow_remote=allow_remote,
            num_predict=num_predict,
        )
    if provider == "mock":
        return make_mock_completion()
    raise ValueError(
        f"graphrag: unknown completion provider {provider!r} (expected 'ollama' or 'mock')"
    )


def get_embedding_func(
    provider: str = "ollama",
    *,
    base_url: str = "http://127.0.0.1:11434",
    model: str = "nomic-embed-text",
    timeout: float = 60.0,
    allow_remote: bool = False,
    mock_dim: int = 64,
) -> tuple[Callable[[Sequence[str]], Awaitable[Any]], int]:
    """Return ``(async_callable, dim)`` for LightRAG's ``EmbeddingFunc``.

    ``provider`` is one of ``"ollama"`` or ``"mock"``.
    """
    if provider == "ollama":
        return make_ollama_embedding(
            base_url, model, timeout=timeout, allow_remote=allow_remote
        )
    if provider == "mock":
        return make_mock_embedding(dim=mock_dim)
    raise ValueError(
        f"graphrag: unknown embedding provider {provider!r} (expected 'ollama' or 'mock')"
    )
