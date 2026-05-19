"""Unit tests for :mod:`pollypm.storage.embedder` (issue #1737, Slice D).

The OpenAI implementation is exercised against a mock ``http_post``
callable so no real API key or network is needed. The registry surface
is tested via :func:`register_embedder` round-trips.
"""

from __future__ import annotations

import json

import pytest

from pollypm.storage.embedder import (
    EmbedderError,
    EmbedderInfo,
    OpenAIEmbedder,
    register_embedder,
    resolve_embedder,
    unregister_embedder,
)


# --------------------------------------------------------------------- #
# Helpers — a fake http_post that records calls and returns canned
# bodies. Each instance owns its own state so tests don't share.
# --------------------------------------------------------------------- #


class _FakeHTTP:
    def __init__(self, *, status: int = 200, body: dict | None = None) -> None:
        self.status = status
        self.body = body or {"data": []}
        self.calls: list[tuple[str, bytes, dict[str, str], float]] = []
        # Allow per-call overrides for retry-flow tests.
        self.queued: list[tuple[int, bytes]] = []

    def __call__(self, url, body, headers, timeout):
        self.calls.append((url, body, headers, timeout))
        if self.queued:
            return self.queued.pop(0)
        return self.status, json.dumps(self.body).encode("utf-8")


def _ok_body(vectors: list[list[float]]) -> dict:
    return {
        "data": [
            {"index": i, "embedding": v, "object": "embedding"}
            for i, v in enumerate(vectors)
        ],
        "model": "text-embedding-3-small",
        "object": "list",
    }


# --------------------------------------------------------------------- #
# Basic OpenAIEmbedder behaviour.
# --------------------------------------------------------------------- #


def test_info_exposes_known_dim():
    e = OpenAIEmbedder(model="text-embedding-3-small", api_key="sk-test")
    assert e.info == EmbedderInfo(model="openai:text-embedding-3-small", dim=1536)


def test_info_exposes_large_dim():
    e = OpenAIEmbedder(model="text-embedding-3-large", api_key="sk-test")
    assert e.info.dim == 3072
    assert e.info.model == "openai:text-embedding-3-large"


def test_embed_happy_path_round_trip():
    http = _FakeHTTP(body=_ok_body([[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]))
    embedder = OpenAIEmbedder(
        api_key="sk-test", http_post=http,
    )
    out = embedder.embed(["alpha", "beta"])
    assert out == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    # One request, with the right body.
    assert len(http.calls) == 1
    sent_body = json.loads(http.calls[0][1])
    assert sent_body["input"] == ["alpha", "beta"]
    assert sent_body["model"] == "text-embedding-3-small"
    assert http.calls[0][2]["Authorization"] == "Bearer sk-test"


def test_embed_empty_list_returns_empty():
    http = _FakeHTTP()
    embedder = OpenAIEmbedder(api_key="sk-test", http_post=http)
    assert embedder.embed([]) == []
    assert http.calls == []


def test_embed_substitutes_empty_strings():
    """OpenAI rejects empty input; the embedder replaces with ' '."""
    http = _FakeHTTP(body=_ok_body([[0.1, 0.2]]))
    embedder = OpenAIEmbedder(api_key="sk-test", http_post=http)
    embedder.embed([""])
    sent = json.loads(http.calls[0][1])
    assert sent["input"] == [" "]


def test_embed_out_of_order_response_is_sorted():
    """OpenAI reserves the right to shuffle; the parser must re-order."""
    payload = {
        "data": [
            {"index": 1, "embedding": [0.2, 0.2], "object": "embedding"},
            {"index": 0, "embedding": [0.1, 0.1], "object": "embedding"},
        ],
        "model": "x",
        "object": "list",
    }
    http = _FakeHTTP(body=payload)
    embedder = OpenAIEmbedder(api_key="sk-test", http_post=http)
    assert embedder.embed(["a", "b"]) == [[0.1, 0.1], [0.2, 0.2]]


def test_embed_batch_too_large_raises():
    embedder = OpenAIEmbedder(api_key="sk-test", http_post=_FakeHTTP())
    too_many = ["x"] * (embedder.max_batch_size + 1)
    with pytest.raises(EmbedderError, match="exceeds cap"):
        embedder.embed(too_many)


# --------------------------------------------------------------------- #
# Auth / API key resolution.
# --------------------------------------------------------------------- #


def test_resolve_key_from_constructor_arg():
    http = _FakeHTTP(body=_ok_body([[0.0]]))
    e = OpenAIEmbedder(api_key="sk-from-ctor", http_post=http)
    e.embed(["x"])
    assert http.calls[0][2]["Authorization"] == "Bearer sk-from-ctor"


def test_resolve_key_from_env(monkeypatch):
    monkeypatch.setenv("CUSTOM_KEY_ENV", "sk-from-env")
    http = _FakeHTTP(body=_ok_body([[0.0]]))
    e = OpenAIEmbedder(
        api_key_env_name="CUSTOM_KEY_ENV", http_post=http,
    )
    e.embed(["x"])
    assert http.calls[0][2]["Authorization"] == "Bearer sk-from-env"


def test_resolve_key_missing_raises(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    e = OpenAIEmbedder(http_post=_FakeHTTP())
    with pytest.raises(EmbedderError, match="no API key"):
        e.embed(["x"])


# --------------------------------------------------------------------- #
# Retry / backoff.
# --------------------------------------------------------------------- #


def test_retries_on_429_then_succeeds():
    http = _FakeHTTP()
    http.queued = [
        (429, b'{"error": "rate-limited"}'),
        (429, b'{"error": "rate-limited"}'),
        (200, json.dumps(_ok_body([[0.5]])).encode("utf-8")),
    ]
    sleeps: list[float] = []
    e = OpenAIEmbedder(
        api_key="sk-test",
        http_post=http,
        max_retries=5,
        backoff_initial=0.01,
        backoff_max=0.1,
        sleep=lambda s: sleeps.append(s),
    )
    result = e.embed(["x"])
    assert result == [[0.5]]
    assert len(http.calls) == 3
    # First two failures slept; third call succeeded.
    assert len(sleeps) == 2
    assert sleeps[0] <= sleeps[1]


def test_retries_exhausted_on_persistent_500():
    http = _FakeHTTP()
    http.queued = [(500, b"err")] * 6
    sleeps: list[float] = []
    e = OpenAIEmbedder(
        api_key="sk-test",
        http_post=http,
        max_retries=3,
        backoff_initial=0.01,
        backoff_max=0.1,
        sleep=lambda s: sleeps.append(s),
    )
    with pytest.raises(EmbedderError, match="HTTP 500"):
        e.embed(["x"])
    # 1 initial + 3 retries = 4 attempts total.
    assert len(http.calls) == 4


def test_non_retryable_4xx_raises_immediately():
    http = _FakeHTTP()
    http.queued = [(401, b'{"error": "unauthorized"}')]
    e = OpenAIEmbedder(
        api_key="sk-test",
        http_post=http,
        max_retries=5,
        backoff_initial=0.01,
        sleep=lambda _s: None,
    )
    with pytest.raises(EmbedderError, match="HTTP 401"):
        e.embed(["x"])
    # One attempt — 401 is non-retryable.
    assert len(http.calls) == 1


def test_network_error_retries():
    import urllib.error

    sleeps: list[float] = []
    attempts = {"n": 0}
    good_body = json.dumps(_ok_body([[0.7]])).encode("utf-8")

    def flaky_post(url, body, headers, timeout):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise urllib.error.URLError("connection refused")
        return 200, good_body

    e = OpenAIEmbedder(
        api_key="sk-test",
        http_post=flaky_post,
        max_retries=5,
        backoff_initial=0.01,
        sleep=lambda s: sleeps.append(s),
    )
    assert e.embed(["x"]) == [[0.7]]
    assert attempts["n"] == 3
    assert len(sleeps) == 2


# --------------------------------------------------------------------- #
# Response shape validation.
# --------------------------------------------------------------------- #


def test_malformed_response_raises():
    http = _FakeHTTP(body={"oops": True})
    e = OpenAIEmbedder(api_key="sk-test", http_post=http)
    with pytest.raises(EmbedderError, match="missing 'data'"):
        e.embed(["x"])


def test_wrong_count_response_raises():
    http = _FakeHTTP(body=_ok_body([[0.1]]))  # 1 vec for 2 inputs
    e = OpenAIEmbedder(api_key="sk-test", http_post=http)
    with pytest.raises(EmbedderError, match="expected 2"):
        e.embed(["a", "b"])


def test_invalid_json_response_raises():
    http = _FakeHTTP()
    http.queued = [(200, b"not json")]
    e = OpenAIEmbedder(api_key="sk-test", http_post=http)
    with pytest.raises(EmbedderError, match="not JSON"):
        e.embed(["x"])


# --------------------------------------------------------------------- #
# Registry surface.
# --------------------------------------------------------------------- #


def test_resolve_embedder_bare_model_uses_openai_default():
    # No colon → openai provider. The default factory will try to
    # build an OpenAIEmbedder; we don't call .embed() so no key
    # check fires.
    embedder = resolve_embedder("text-embedding-3-small", "OPENAI_API_KEY")
    assert isinstance(embedder, OpenAIEmbedder)
    assert embedder.model == "text-embedding-3-small"


def test_resolve_embedder_dispatches_by_provider():
    seen: list[tuple[str, str]] = []

    class Stub:
        @property
        def info(self):
            return EmbedderInfo(model="stub:x", dim=4)

        @property
        def max_batch_size(self):
            return 10

        def embed(self, texts):
            return [[0.0] * 4 for _ in texts]

    def factory(model: str, api_key_env: str):
        seen.append((model, api_key_env))
        return Stub()

    try:
        register_embedder("stubprov", factory)
        e = resolve_embedder("stubprov:my-model", "MY_KEY")
        assert isinstance(e, Stub)
        assert seen == [("my-model", "MY_KEY")]
    finally:
        unregister_embedder("stubprov")


def test_resolve_embedder_unknown_provider_raises():
    with pytest.raises(EmbedderError, match="no factory"):
        resolve_embedder("nope:foo", "X")
