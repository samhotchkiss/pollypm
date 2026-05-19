"""Embedding-provider abstraction for pgvector recall (issue #1737, Slice D).

The :class:`Embedder` Protocol is a tiny synchronous batch surface — give
it a list of strings, get back a list of float vectors. The default
implementation :class:`OpenAIEmbedder` calls OpenAI's embeddings API with
exponential backoff on transient errors.

Why a Protocol + registry
-------------------------

Sam wants the option to swap to a local model (``nomic-embed-text`` via
Ollama, etc.) without rewriting the embedding-on-write path. The
Protocol pins the contract; the registry resolves a provider-namespaced
model name (``openai:text-embedding-3-small``) to a concrete class so
``[storage.embedding] model = "..."`` is the single switch. Slice D
only ships the OpenAI implementation; the registry is the seam future
providers slot into.

Why sync
--------

PollyPM's storage layer is sync (psycopg 3 sync, ``threading.Lock``).
Making the embedder async would force every caller (background writer
thread, recall API, backfill CLI) to either spawn an event loop or wrap
``asyncio.run`` per batch. The OpenAI client supports sync calls
natively; we use ``urllib`` instead so the package doesn't grow another
runtime dep just for this slice.

Network errors
--------------

Transient HTTP failures (429, 500, 502, 503, 504, network timeouts) are
retried with exponential backoff up to ``max_retries``. Non-retryable
errors (auth, bad-request shape) raise :class:`EmbedderError` straight
through so the caller (background writer) logs + skips rather than
spinning forever.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Protocol


logger = logging.getLogger(__name__)


# --------------------------------------------------------------------- #
# Public Protocol + result types.
# --------------------------------------------------------------------- #


class EmbedderError(RuntimeError):
    """Embedder failed in a non-retryable way (auth, bad shape, etc.).

    Background writer + backfill catch this, log, and skip the row.
    """


@dataclass(slots=True, frozen=True)
class EmbedderInfo:
    """Static metadata about an embedder.

    Used by the writer to stamp the ``embeddings.model`` column and
    by the schema to verify the embedding dim matches ``vector(N)``.
    """

    model: str
    dim: int


class Embedder(Protocol):
    """Batch text -> dense vectors.

    All implementations MUST:

    * Accept any non-empty list of strings up to
      :attr:`max_batch_size` long.
    * Return a list of float lists in the same order as the input.
    * Each vector MUST have exactly :attr:`info.dim` entries.
    * Raise :class:`EmbedderError` on permanent failure;
      transient errors should be retried internally.
    """

    @property
    def info(self) -> EmbedderInfo: ...

    @property
    def max_batch_size(self) -> int: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


# --------------------------------------------------------------------- #
# OpenAI implementation.
# --------------------------------------------------------------------- #


# OpenAI's documented per-request cap is 2048 inputs but the practical
# soft limit lives lower (rate-limit token math + per-request latency).
# 100 keeps every batch comfortably under the per-minute token rate
# limit while still amortising the HTTP overhead. The writer batches
# at 32 (per #1737 spec) so this is a generous upper bound.
OPENAI_BATCH_CAP = 100

_OPENAI_MODEL_DIMS: dict[str, int] = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}

# HTTP status codes that signal "try again later". 408 (request timeout),
# 429 (rate-limited), and 5xx server errors are all retryable; anything
# else (auth, bad request) crashes out as non-retryable.
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


@dataclass(slots=True)
class OpenAIEmbedder:
    """OpenAI ``v1/embeddings`` implementation of :class:`Embedder`.

    Constructor params:

    ``model`` — bare model id (no ``openai:`` prefix). Use
        ``"text-embedding-3-small"`` for the default knob.
    ``api_key`` — API key. ``None`` means resolve from the env var
        named by ``api_key_env_name`` at call time.
    ``api_key_env_name`` — env var to look up when ``api_key`` is
        ``None``. Defaults to ``"OPENAI_API_KEY"``.
    ``api_base`` — override for the API base URL. Useful for tests
        or self-hosted proxies. Defaults to OpenAI production.
    ``max_retries`` — number of retry attempts on transient errors.
    ``backoff_initial`` / ``backoff_max`` — exponential-backoff
        bounds, in seconds.
    ``timeout`` — per-request HTTP timeout.
    ``http_post`` — injection seam for tests; pass a callable with
        signature ``(url, body_bytes, headers, timeout) -> (status,
        body_bytes)``. Defaults to a :mod:`urllib`-backed helper.
    """

    model: str = "text-embedding-3-small"
    api_key: str | None = None
    api_key_env_name: str = "OPENAI_API_KEY"
    api_base: str = "https://api.openai.com/v1"
    max_retries: int = 5
    backoff_initial: float = 0.5
    backoff_max: float = 30.0
    timeout: float = 30.0
    http_post: Callable[
        [str, bytes, dict[str, str], float], tuple[int, bytes]
    ] | None = None
    sleep: Callable[[float], None] = field(default=time.sleep)

    @property
    def info(self) -> EmbedderInfo:
        dim = _OPENAI_MODEL_DIMS.get(self.model, 1536)
        return EmbedderInfo(model=f"openai:{self.model}", dim=dim)

    @property
    def max_batch_size(self) -> int:
        return OPENAI_BATCH_CAP

    def _resolve_key(self) -> str:
        if self.api_key:
            return self.api_key
        env_value = os.environ.get(self.api_key_env_name, "").strip()
        if not env_value:
            raise EmbedderError(
                f"OpenAI embedder: no API key in ${self.api_key_env_name}; "
                "set the env var or pass api_key=... to the constructor."
            )
        return env_value

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if len(texts) > self.max_batch_size:
            raise EmbedderError(
                f"OpenAI embedder: batch of {len(texts)} exceeds cap of "
                f"{self.max_batch_size}; chunk before calling embed()."
            )
        # OpenAI rejects empty inputs with 400. Replace empty strings
        # with a single space so the writer's "embed everything"
        # contract still holds — empty memory entries are real (a row
        # might be a title-only stub) and we'd rather embed a stand-in
        # than fail the whole batch.
        sanitized = [t if t.strip() else " " for t in texts]

        api_key = self._resolve_key()
        url = f"{self.api_base.rstrip('/')}/embeddings"
        body = json.dumps({"model": self.model, "input": sanitized}).encode(
            "utf-8"
        )
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        post = self.http_post or _urllib_post
        attempt = 0
        delay = self.backoff_initial
        while True:
            attempt += 1
            try:
                status, response_body = post(url, body, headers, self.timeout)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt > self.max_retries:
                    raise EmbedderError(
                        f"OpenAI embedder: network failure after "
                        f"{attempt} attempts: {exc!r}"
                    ) from exc
                logger.warning(
                    "embedder: network error attempt=%d/%d err=%r; "
                    "sleeping %.2fs",
                    attempt,
                    self.max_retries,
                    exc,
                    delay,
                )
                self.sleep(delay)
                delay = min(delay * 2, self.backoff_max)
                continue

            if 200 <= status < 300:
                return _parse_embedding_response(response_body, len(sanitized))

            if status in _RETRYABLE_STATUS and attempt <= self.max_retries:
                logger.warning(
                    "embedder: transient HTTP %d attempt=%d/%d; "
                    "sleeping %.2fs",
                    status,
                    attempt,
                    self.max_retries,
                    delay,
                )
                self.sleep(delay)
                delay = min(delay * 2, self.backoff_max)
                continue

            snippet = response_body[:512].decode("utf-8", errors="replace")
            raise EmbedderError(
                f"OpenAI embedder: HTTP {status} after attempt {attempt}: "
                f"{snippet}"
            )


def _urllib_post(
    url: str,
    body: bytes,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, bytes]:
    """Default HTTP POST via :mod:`urllib`. Returns ``(status, body)``.

    Both successful and HTTP-error responses are surfaced as
    ``(status, body)`` so the caller's retry loop can inspect the
    status code. Network/timeout failures still raise — those are
    caught upstream and counted against the retry budget.
    """
    request = urllib.request.Request(  # noqa: S310 — fixed scheme, no user input
        url,
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 — fixed scheme
            request, timeout=timeout
        ) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() if exc.fp else b""


def _parse_embedding_response(body: bytes, expected: int) -> list[list[float]]:
    """Parse OpenAI's ``v1/embeddings`` response into a list of vectors.

    Raises :class:`EmbedderError` on missing fields or wrong shape so
    a malformed proxy response doesn't silently produce zero-length
    vectors.
    """
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise EmbedderError(
            f"OpenAI embedder: response was not JSON: {exc!r}"
        ) from exc

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise EmbedderError(
            "OpenAI embedder: response missing 'data' array; "
            f"got keys {list(payload) if isinstance(payload, dict) else payload!r}"
        )
    if len(data) != expected:
        raise EmbedderError(
            f"OpenAI embedder: expected {expected} embeddings, "
            f"got {len(data)}"
        )

    # OpenAI returns rows with an ``index`` field; sort by it to be
    # robust to out-of-order responses (the API documents in-order
    # but the spec allows for shuffle).
    vectors: list[list[float] | None] = [None] * expected
    for row in data:
        if not isinstance(row, dict):
            raise EmbedderError(
                "OpenAI embedder: malformed row in response data"
            )
        idx = row.get("index", 0)
        embedding = row.get("embedding")
        if not isinstance(embedding, list):
            raise EmbedderError(
                "OpenAI embedder: row missing 'embedding' list"
            )
        try:
            float_vec = [float(x) for x in embedding]
        except (TypeError, ValueError) as exc:
            raise EmbedderError(
                f"OpenAI embedder: non-float entry in embedding: {exc!r}"
            ) from exc
        if not isinstance(idx, int) or not (0 <= idx < expected):
            raise EmbedderError(
                f"OpenAI embedder: bad index {idx!r} in response"
            )
        vectors[idx] = float_vec

    if any(v is None for v in vectors):
        raise EmbedderError(
            "OpenAI embedder: response had gaps in index sequence"
        )
    return vectors  # type: ignore[return-value]


# --------------------------------------------------------------------- #
# Registry — resolve provider-namespaced model names to a class.
# --------------------------------------------------------------------- #


# A factory takes the bare model name (without the ``provider:`` prefix)
# and an env-var name; returns a constructed :class:`Embedder`. Tests
# register stubs via :func:`register_embedder` to inject canned vectors.
EmbedderFactory = Callable[[str, str], Embedder]


_EMBEDDER_REGISTRY: dict[str, EmbedderFactory] = {}


def register_embedder(provider: str, factory: EmbedderFactory) -> None:
    """Register a provider factory under ``provider`` (e.g. ``"openai"``).

    Replaces any existing entry — the registry is intentionally
    last-write-wins so tests can override the real OpenAI factory
    with a stub.
    """
    _EMBEDDER_REGISTRY[provider] = factory


def unregister_embedder(provider: str) -> None:
    """Remove ``provider`` from the registry. Idempotent."""
    _EMBEDDER_REGISTRY.pop(provider, None)


def _default_openai_factory(model: str, api_key_env: str) -> Embedder:
    return OpenAIEmbedder(model=model, api_key_env_name=api_key_env)


# Bootstrap the OpenAI default. Tests that need a deterministic
# embedder call :func:`register_embedder` to override.
register_embedder("openai", _default_openai_factory)


def resolve_embedder(model_spec: str, api_key_env: str) -> Embedder:
    """Resolve ``provider:model`` -> :class:`Embedder` via the registry.

    Parameters
    ----------
    model_spec:
        A provider-namespaced model name such as
        ``"openai:text-embedding-3-small"``. The portion before the
        first colon is the provider key; the rest is the model id
        passed to the factory. A bare model name (no colon) is
        treated as the ``openai`` provider — preserves backwards
        compatibility with installs that haven't updated their
        config since pre-Slice-D.
    api_key_env:
        Name of the env var that holds the provider's API key.
        Passed through to the factory.
    """
    if ":" in model_spec:
        provider, _, model = model_spec.partition(":")
    else:
        provider, model = "openai", model_spec
    factory = _EMBEDDER_REGISTRY.get(provider)
    if factory is None:
        raise EmbedderError(
            f"embedder: no factory registered for provider "
            f"{provider!r}; known: {sorted(_EMBEDDER_REGISTRY)}"
        )
    return factory(model, api_key_env)


__all__ = [
    "Embedder",
    "EmbedderError",
    "EmbedderFactory",
    "EmbedderInfo",
    "OpenAIEmbedder",
    "register_embedder",
    "resolve_embedder",
    "unregister_embedder",
]
