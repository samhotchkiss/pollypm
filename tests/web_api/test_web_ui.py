"""V0 web UI tests (Phase 7).

Covers:

- ``GET /ui/`` returns 200 + sets the ``pollypm-session`` cookie from
  the on-disk token.
- ``GET /ui/`` HTML carries the anchors the SPA needs
  (surface-list, message-list, send-input).
- Static assets ``app.js`` / ``styles.css`` are served.
- Auth-via-cookie works (no Authorization header).
- Auth-via-Tailscale-CGNAT works (no header, no cookie).
- Auth without anything still returns 401.
- ``pm serve --tailscale`` warns + falls back to localhost when the
  tailscale binary is missing.

Tests use the shared fixtures in ``conftest.py``; the ``client``
fixture builds the FastAPI app via ``create_app`` so the UI mount
flows through the same code path ``pm serve`` uses at runtime.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from pollypm.cli_features.web_api import detect_tailscale_ip
from pollypm.web_api.auth import SESSION_COOKIE_NAME, is_tailscale_ip


def test_ui_root_returns_html_and_sets_session_cookie(client: TestClient, token: str) -> None:
    """``GET /ui/`` returns the SPA HTML and seeds the session cookie."""
    response = client.get("/ui/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    cookie = response.cookies.get(SESSION_COOKIE_NAME)
    assert cookie == token, "session cookie should mirror on-disk token"


def test_ui_index_contains_required_anchors(client: TestClient) -> None:
    """The HTML must carry the IDs ``app.js`` queries for."""
    html = client.get("/ui/").text
    for anchor in ("surface-list", "message-list", "send-input", "send-button"):
        assert f'id="{anchor}"' in html, f"missing anchor #{anchor} in index.html"
    # The cookie-based credentials assumption is baked into app.js; the
    # HTML must reference it so cache-busting / rename doesn't silently
    # break the SPA.
    assert "/ui/app.js" in html
    assert "/ui/styles.css" in html


def test_ui_static_js_served(client: TestClient) -> None:
    """``GET /ui/app.js`` returns the vanilla JS app."""
    response = client.get("/ui/app.js")
    assert response.status_code == 200
    body = response.text
    assert "credentials" in body, "app.js must use credentials:'include'"
    assert "loadSurfaces" in body
    assert "loadHistory" in body
    assert "sendMessage" in body


def test_ui_static_css_served(client: TestClient) -> None:
    """``GET /ui/styles.css`` returns the dark-theme stylesheet."""
    response = client.get("/ui/styles.css")
    assert response.status_code == 200
    body = response.text
    # Sanity-check that the palette wired through (not an empty file).
    assert "--info" in body or "#5b8aff" in body


def test_auth_via_cookie_works(client: TestClient, token: str) -> None:
    """A request that carries only the session cookie authenticates."""
    # Boot through /ui/ so the cookie is set on the TestClient's jar,
    # then call a JSON endpoint with no Authorization header.
    boot = client.get("/ui/")
    assert boot.status_code == 200
    response = client.get("/api/v1/projects")  # no headers — cookie only
    assert response.status_code == 200


def test_auth_via_tailscale_cgnat_ip_works(api_config, token_path: Path) -> None:
    """A request from a 100.64.0.0/10 client IP bypasses cred checks."""
    from pollypm.web_api import create_app

    app = create_app(config=api_config, token_path=token_path)
    # FastAPI's TestClient lets us spoof the client host via
    # ``base_url`` only at TLS level; the cleaner path is to override
    # the ``Request.client`` host through the ``client`` arg of
    # ``TestClient``.
    tail_client = TestClient(app, base_url="http://testserver")
    # ``TestClient`` honors ``client=("host", port)`` so the ASGI
    # scope reports the simulated peer.
    tail_client.headers.clear()
    resp = tail_client.get(
        "/api/v1/projects",
        headers={},
        # No bearer, no cookie — Tailscale-only.
    )
    # Without the override the default test client uses ``testclient``
    # as the peer, which is NOT a tailnet IP, so this 401s. Use a
    # manual ASGI call to set the peer.
    assert resp.status_code == 401  # baseline: no creds, no tailnet IP

    # Now do it properly through a custom transport. ``ASGITransport``
    # accepts a ``client`` tuple that flows into the ASGI scope as
    # ``client=("100.64.0.5", port)``, which is what
    # ``request.client.host`` reads inside the auth dependency.
    import asyncio

    import httpx

    async def _probe() -> httpx.Response:
        transport = httpx.ASGITransport(app=app, client=("100.64.0.5", 12345))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver",
        ) as ac:
            return await ac.get("/api/v1/projects")

    resp2 = asyncio.run(_probe())
    assert resp2.status_code == 200, (
        "Tailscale CGNAT IP (100.64.0.5) should bypass credential check; "
        f"got {resp2.status_code} {resp2.text}"
    )


def test_auth_without_any_credentials_returns_401(client: TestClient) -> None:
    """No cookie, no header, non-tailnet peer → 401 unauthorized."""
    # TestClient defaults to client=('testclient', 50000) which is not
    # a CGNAT IP, so this exercises the rejection path.
    response = client.get("/api/v1/projects")
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "unauthorized"


def test_is_tailscale_ip_helper() -> None:
    """``is_tailscale_ip`` accepts 100.64.0.0/10 and rejects everything else."""
    assert is_tailscale_ip("100.64.0.5") is True
    assert is_tailscale_ip("100.127.255.254") is True
    assert is_tailscale_ip("127.0.0.1") is False
    assert is_tailscale_ip("192.168.1.5") is False
    assert is_tailscale_ip("10.0.0.1") is False
    assert is_tailscale_ip(None) is False
    assert is_tailscale_ip("") is False
    assert is_tailscale_ip("not-an-ip") is False
    # IPv6 outside CGNAT (CGNAT is IPv4-only) → False.
    assert is_tailscale_ip("::1") is False


def test_detect_tailscale_ip_returns_none_when_binary_missing() -> None:
    """``detect_tailscale_ip`` collapses missing-binary to None."""
    with patch(
        "pollypm.cli_features.web_api.shutil.which",
        return_value=None,
    ):
        assert detect_tailscale_ip() is None


def test_pm_serve_tailscale_warns_when_binary_missing(
    api_config, token_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """``pm serve --tailscale`` without tailscale prints a warning + falls back.

    Drives the CLI command via Typer's runner so the user-visible
    stderr matches what the operator will see in their terminal.
    """
    import typer
    from typer.testing import CliRunner

    from pollypm.cli_features.web_api import register_web_api_commands

    root = typer.Typer()
    register_web_api_commands(root)

    # Stub uvicorn so the test doesn't actually open a socket. The
    # serve command imports uvicorn inside the function body, so we
    # monkeypatch the module attribute at import time.
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: None)
    # Force ``shutil.which("tailscale")`` to miss.
    monkeypatch.setattr(
        "pollypm.cli_features.web_api.shutil.which",
        lambda name: None,
    )
    # Stub config + token machinery so we don't depend on the user's
    # real ~/.pollypm/.
    monkeypatch.setattr(
        "pollypm.cli_features.web_api.load_config",
        lambda _path: api_config,
    )
    monkeypatch.setattr(
        "pollypm.web_api.ensure_token",
        lambda _path: ("test-token", False),
    )

    runner = CliRunner()
    result = runner.invoke(
        root,
        ["serve", "--tailscale", "--token-path", str(token_path)],
    )
    assert result.exit_code == 0, result.output
    # CliRunner merges stderr into ``output`` by default; the
    # ``[pm serve]`` banner + warning land in stderr via typer.echo.
    assert "--tailscale" in result.output
    assert "tailscale ip -4" in result.output or "Falling back" in result.output
