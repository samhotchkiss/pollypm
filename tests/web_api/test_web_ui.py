"""V0 web UI tests (Phase 7).

Covers:

- ``GET /ui/`` returns 200 + sets the ``pollypm-session`` cookie from
  the on-disk token **only** for trusted callers (loopback / Tailscale
  / valid bearer).
- ``GET /ui/`` from a LAN client gets the HTML but no cookie, and a
  subsequent ``/api/`` call returns 401.
- ``GET /ui/`` HTML carries the anchors the SPA needs
  (surface-list, message-list, send-input).
- Static assets ``app.js`` / ``styles.css`` are served.
- Auth-via-cookie works (no Authorization header).
- Auth-via-Tailscale-CGNAT works (no header, no cookie).
- Auth without anything still returns 401.
- ``pm serve`` (default) binds the detected Tailscale IPv4 only;
  ``--tailscale`` is a no-op compat flag; falls back to loopback when
  the tailscale binary is missing.
- Mobile CSS media query exists.

Tests use the shared fixtures in ``conftest.py``; the ``client``
fixture builds the FastAPI app via ``create_app`` so the UI mount
flows through the same code path ``pm serve`` uses at runtime.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from pollypm.cli_features.web_api import detect_tailscale_ip
from pollypm.web_api.auth import SESSION_COOKIE_NAME, is_tailscale_ip


def _ui_get_with_peer(app, peer_ip: str, *, headers: dict[str, str] | None = None) -> httpx.Response:
    """Fetch ``GET /ui/`` with a simulated ``request.client.host``.

    The default ``TestClient`` uses ``("testclient", 50000)`` as the
    peer, which doesn't exercise the loopback / Tailscale gate. We
    use ``httpx.ASGITransport(client=…)`` to push a real-looking peer
    tuple into the ASGI scope.
    """

    async def _probe() -> httpx.Response:
        transport = httpx.ASGITransport(app=app, client=(peer_ip, 12345))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver",
        ) as ac:
            return await ac.get("/ui/", headers=headers or {})

    return asyncio.run(_probe())


def _api_get_with_peer(app, peer_ip: str, *, headers: dict[str, str] | None = None) -> httpx.Response:
    """Same as ``_ui_get_with_peer`` but for the JSON API."""

    async def _probe() -> httpx.Response:
        transport = httpx.ASGITransport(app=app, client=(peer_ip, 12345))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver",
        ) as ac:
            return await ac.get("/api/v1/projects", headers=headers or {})

    return asyncio.run(_probe())


def test_ui_root_returns_html_and_sets_session_cookie(client: TestClient, token: str) -> None:
    """``GET /ui/`` returns the SPA HTML and seeds the session cookie.

    The ``TestClient`` default peer is ``testclient`` which our gate
    accepts as ``request.client.host == "testclient"`` is neither
    loopback nor tailnet — but FastAPI's TestClient actually sets the
    scope client to ``("testclient", 50000)``. To keep this baseline
    test green we explicitly supply the Authorization header so the
    cookie is issued via the valid-bearer path.
    """
    response = client.get("/ui/", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    cookie = response.cookies.get(SESSION_COOKIE_NAME)
    assert cookie == token, "session cookie should mirror on-disk token"


# -------- P0 #1: cookie issuance gating ---------------------------------


def test_ui_no_cookie_from_lan_client(app, token: str) -> None:
    """A LAN client (non-loopback, non-tailnet, no bearer) gets the
    HTML but no Set-Cookie header — and the next /api/ call 401s.
    """
    resp = _ui_get_with_peer(app, "192.168.1.100")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert SESSION_COOKIE_NAME not in resp.cookies, (
        f"LAN client must NOT receive Set-Cookie; got cookies: {dict(resp.cookies)}"
    )
    # And the SPA's first /api/ call should hit 401.
    api_resp = _api_get_with_peer(app, "192.168.1.100")
    assert api_resp.status_code == 401


def test_ui_cookie_set_from_loopback(app, token: str) -> None:
    """Loopback peer is trusted — cookie is issued."""
    resp = _ui_get_with_peer(app, "127.0.0.1")
    assert resp.status_code == 200
    assert resp.cookies.get(SESSION_COOKIE_NAME) == token


def test_ui_cookie_set_from_tailscale_peer(app, token: str) -> None:
    """Tailscale CGNAT peer is trusted — cookie is issued."""
    resp = _ui_get_with_peer(app, "100.64.0.5")
    assert resp.status_code == 200
    assert resp.cookies.get(SESSION_COOKIE_NAME) == token


def test_ui_cookie_set_with_valid_bearer_header(app, token: str) -> None:
    """A valid Authorization header from any peer gets the cookie."""
    resp = _ui_get_with_peer(
        app,
        "192.168.1.100",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.cookies.get(SESSION_COOKIE_NAME) == token


def test_ui_no_cookie_with_invalid_bearer(app, token: str) -> None:
    """A bogus bearer header from an untrusted peer does NOT issue."""
    resp = _ui_get_with_peer(
        app,
        "192.168.1.100",
        headers={"Authorization": "Bearer not-the-real-token"},
    )
    assert resp.status_code == 200
    assert SESSION_COOKIE_NAME not in resp.cookies


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
    """A request that carries only the session cookie authenticates.

    Bootstrap with a valid bearer header (TestClient's default peer
    is ``("testclient", 50000)``, neither loopback nor tailnet, so
    only the bearer path issues a cookie). Then drop the header and
    confirm the cookie alone keeps the session live.
    """
    boot = client.get("/ui/", headers={"Authorization": f"Bearer {token}"})
    assert boot.status_code == 200
    assert boot.cookies.get(SESSION_COOKIE_NAME) == token
    response = client.get("/api/v1/projects")  # no headers — cookie only
    assert response.status_code == 200


def test_auth_via_tailscale_cgnat_ip_works(api_config, token_path: Path) -> None:
    """A request from a 100.64.0.0/10 client IP bypasses cred checks."""
    from pollypm.web_api import create_app

    app = create_app(config=api_config, token_path=token_path)
    # Baseline: TestClient default peer is ``testclient`` (not a
    # tailnet IP), so without creds we expect 401.
    baseline_client = TestClient(app, base_url="http://testserver")
    baseline_client.headers.clear()
    baseline_resp = baseline_client.get("/api/v1/projects", headers={})
    assert baseline_resp.status_code == 401  # baseline: no creds, no tailnet IP

    # Now spoof a tailnet peer through an explicit ASGITransport
    # client tuple. ``request.client.host`` inside the auth dependency
    # reads this value.
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


def _run_serve_command(
    api_config,
    token_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    detect_result: str | None,
    extra_args: list[str] | None = None,
) -> tuple[object, dict[str, object]]:
    """Drive ``pm serve`` through Typer's runner with all I/O stubbed.

    Returns ``(CliRunner.Result, uvicorn_kwargs)`` so tests can
    inspect both stderr-merged output and the exact host/port the
    serve command tried to bind.
    """
    import typer
    import uvicorn
    from typer.testing import CliRunner

    from pollypm.cli_features.web_api import register_web_api_commands

    root = typer.Typer()
    register_web_api_commands(root)

    captured: dict[str, object] = {}

    def _fake_uvicorn_run(*args, **kwargs):
        # uvicorn.run(app_instance, host=…, port=…, log_level=…)
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(uvicorn, "run", _fake_uvicorn_run)
    monkeypatch.setattr(
        "pollypm.cli_features.web_api.detect_tailscale_ip",
        lambda: detect_result,
    )
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
        ["serve", "--token-path", str(token_path), *(extra_args or [])],
    )
    return result, captured


# -------- P0 #2 + #3: bind mode (always-detect Tailscale) ---------------


def test_pm_serve_binds_tailscale_ip_when_detected(
    api_config, token_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When Tailscale is detected, uvicorn binds the tailnet IPv4 only."""
    result, captured = _run_serve_command(
        api_config,
        token_path,
        monkeypatch,
        detect_result="100.64.0.5",
    )
    assert result.exit_code == 0, result.output
    assert captured["kwargs"]["host"] == "100.64.0.5", (
        f"expected bind to detected Tailscale IP; got {captured['kwargs']}"
    )
    # Banner should mention the tailscale mode + the IP.
    assert "100.64.0.5" in result.output
    assert "tailscale mode" in result.output


def test_pm_serve_binds_loopback_when_no_tailscale(
    api_config, token_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No Tailscale → fall back to loopback-only bind."""
    result, captured = _run_serve_command(
        api_config,
        token_path,
        monkeypatch,
        detect_result=None,
    )
    assert result.exit_code == 0, result.output
    assert captured["kwargs"]["host"] == "127.0.0.1", (
        f"expected loopback bind; got {captured['kwargs']}"
    )
    assert "loopback only" in result.output


def test_pm_serve_tailscale_flag_is_noop(
    api_config, token_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--tailscale`` is preserved for back-compat but doesn't change behaviour.

    The default ``pm serve`` already auto-detects Tailscale; passing
    the flag must therefore produce the same bind as omitting it.
    """
    result_with_flag, captured_flag = _run_serve_command(
        api_config,
        token_path,
        monkeypatch,
        detect_result="100.64.0.5",
        extra_args=["--tailscale"],
    )
    result_no_flag, captured_no_flag = _run_serve_command(
        api_config,
        token_path,
        monkeypatch,
        detect_result="100.64.0.5",
    )
    assert result_with_flag.exit_code == 0, result_with_flag.output
    assert result_no_flag.exit_code == 0, result_no_flag.output
    assert captured_flag["kwargs"]["host"] == captured_no_flag["kwargs"]["host"]
    assert captured_flag["kwargs"]["host"] == "100.64.0.5"


def test_pm_serve_tailscale_warns_when_binary_missing(
    api_config, token_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``pm serve --tailscale`` without tailscale prints a warning + falls back.

    Drives the CLI command via Typer's runner so the user-visible
    stderr matches what the operator will see in their terminal.
    """
    result, captured = _run_serve_command(
        api_config,
        token_path,
        monkeypatch,
        detect_result=None,
        extra_args=["--tailscale"],
    )
    assert result.exit_code == 0, result.output
    # Banner mentions the fallback path.
    assert "loopback" in result.output or "Falling back" in result.output
    # And the warning explicitly calls out --tailscale.
    assert "--tailscale" in result.output
    assert captured["kwargs"]["host"] == "127.0.0.1"


# -------- P1: mobile CSS -----------------------------------------------


def test_styles_have_mobile_media_query(client: TestClient) -> None:
    """``styles.css`` ships a mobile/tablet collapse breakpoint."""
    body = client.get("/ui/styles.css").text
    assert "@media (max-width: 768px)" in body, (
        "expected mobile/tablet media query for phone Tailscale users"
    )
