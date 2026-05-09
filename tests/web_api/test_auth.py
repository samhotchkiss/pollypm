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
