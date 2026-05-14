"""Bearer-token auth tests.

Covers spec §3:
- Missing token → 401 ``unauthorized``.
- Wrong token → 401 ``invalid_token``.
- Valid token → 200.
- Rotated token invalidates the previous value (no in-memory cache).
- ``/health`` is exempt.
"""

from __future__ import annotations

from pollypm.web_api.token import regenerate_token


def test_health_does_not_require_auth(client) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert "schema_version" in body


def test_request_without_token_returns_401_unauthorized(client) -> None:
    response = client.get("/api/v1/projects")
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "unauthorized"


def test_request_with_wrong_token_returns_401_invalid_token(client) -> None:
    response = client.get(
        "/api/v1/projects",
        headers={"Authorization": "Bearer not-the-real-token"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


def test_request_with_malformed_header_returns_401_unauthorized(client) -> None:
    response = client.get(
        "/api/v1/projects",
        headers={"Authorization": "wrong-scheme value"},
    )
    assert response.status_code == 401
    # Missing bearer prefix is treated as missing.
    assert response.json()["error"]["code"] == "unauthorized"


def test_valid_token_allows_request(client, auth_headers) -> None:
    response = client.get("/api/v1/projects", headers=auth_headers)
    assert response.status_code == 200


def test_rotated_token_invalidates_previous(client, auth_headers, token_path) -> None:
    """Spec §3: ``pm api regen-token`` invalidates all prior tokens."""
    # Old token works.
    assert client.get("/api/v1/projects", headers=auth_headers).status_code == 200
    # Rotate.
    new_token = regenerate_token(token_path)
    # Old header rejected.
    assert client.get("/api/v1/projects", headers=auth_headers).status_code == 401
    # New token accepted.
    assert (
        client.get(
            "/api/v1/projects",
            headers={"Authorization": f"Bearer {new_token}"},
        ).status_code
        == 200
    )


def test_sse_accepts_token_via_query_param(client, token, monkeypatch) -> None:
    """Browser ``EventSource`` cannot send Authorization headers.

    Spec §4 lets SSE clients pass the bearer token via ``?token=``.
    Confirm the SSE endpoint accepts that fallback (and only the
    SSE endpoint — query-string tokens stay out of regular routes).
    """
    # Speed up the keepalive cadence so this test doesn't hang on the
    # 15s default; aligning with the SSE-test fixture.
    monkeypatch.setattr("pollypm.web_api.sse.KEEPALIVE_INTERVAL_S", 0.1)
    monkeypatch.setattr("pollypm.web_api.sse.TAIL_POLL_INTERVAL_S", 0.02)
    monkeypatch.setattr("pollypm.web_api.sse.MAX_STREAM_DURATION_S", 0.5)

    # No header — query token alone should authorize the SSE stream.
    with client.stream("GET", f"/api/v1/events?token={token}") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        # Drain a small amount so the response closes cleanly.
        for _ in response.iter_raw():
            break

    # Wrong query token → 401.
    response = client.get("/api/v1/events?token=not-the-token")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"

    # Query token must NOT be honored on non-SSE routes — those use
    # the header-only auth dependency.
    response = client.get(f"/api/v1/projects?token={token}")
    assert response.status_code == 401
