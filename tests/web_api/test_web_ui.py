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
import json
import shutil
import subprocess
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


def test_ui_cookie_set_from_tailscale_peer(tailnet_app, token: str) -> None:
    """Tailscale CGNAT peer is trusted when tailnet trust is enabled."""
    resp = _ui_get_with_peer(tailnet_app, "100.64.0.5")
    assert resp.status_code == 200
    assert resp.cookies.get(SESSION_COOKIE_NAME) == token


def test_ui_no_cookie_minted_from_cgnat_when_trust_disabled(
    app, token: str  # noqa: ARG001 — token fixture writes the on-disk token
) -> None:
    """CGNAT peer hitting the default app gets HTML but no Set-Cookie.

    Sister to ``test_ui_no_cookie_from_lan_client``: the default
    ``create_app(tailnet_trust_enabled=False)`` (what ``pm serve``
    builds whenever it didn't bind to a verified Tailscale IPv4)
    must NOT mint a session cookie just because the source IP looks
    like a tailnet address. RFC 6598 shared address space is also used
    by some ISP CGNATs; trusting it unconditionally was the round-2
    Codex blocker.
    """
    resp = _ui_get_with_peer(app, "100.64.0.5")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert SESSION_COOKIE_NAME not in resp.cookies, (
        "CGNAT peer must NOT receive Set-Cookie when tailnet trust is "
        f"disabled; got cookies: {dict(resp.cookies)}"
    )


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
    for anchor in (
        "surface-list",
        "message-list",
        "send-input",
        "stop-agent-button",
        "send-button",
    ):
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


def test_ui_app_js_uses_sse_refresh_with_polling_fallback(
    client: TestClient,
) -> None:
    """The SPA refresh loop is driven by ``/events`` with a bounded fallback."""
    body = client.get("/ui/app.js").text
    assert "EventSource" in body
    assert 'API + "/events"' in body
    assert "SSE_FAILURE_LIMIT" in body
    assert "startFallbackPolling" in body
    assert "handleSseEvent" in body
    assert "POLL_MESSAGES_MS" not in body
    assert "POLL_DASHBOARD_MS" not in body
    assert "setInterval(pollDashboard" not in body


def test_ui_static_css_served(client: TestClient) -> None:
    """``GET /ui/styles.css`` returns the dark-theme stylesheet."""
    response = client.get("/ui/styles.css")
    assert response.status_code == 200
    body = response.text
    # Sanity-check that the palette wired through (not an empty file).
    assert "--info" in body or "#5b8aff" in body


def test_health_reports_bearer_only_auth_mode(client: TestClient) -> None:
    """Default app health reports the closed-by-default auth posture."""
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["auth_mode"] == "bearer_only"
    assert body["tailnet_trust_enabled"] is False


def test_health_reports_tailnet_trust_mode(tailnet_app) -> None:
    """Tailnet-bound app health reports credential-free tailnet trust."""
    response = TestClient(tailnet_app).get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["auth_mode"] == "tailnet_trust"
    assert body["tailnet_trust_enabled"] is True


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


def test_cgnat_trust_enabled_allows_credential_free_from_tailnet(
    api_config, token_path: Path,
) -> None:
    """Positive test: tailnet-bound mode lets CGNAT peers in unauthenticated.

    Built with ``tailnet_trust_enabled=True`` — the same mode
    ``pm serve`` enables when it actually bound to a verified
    Tailscale IPv4 (see ``cli_features/web_api.py`` round-2). Mirrors
    the production wire path so the credential-free convenience for
    tailnet users keeps working in v0.
    """
    from pollypm.web_api import create_app

    app = create_app(
        config=api_config,
        token_path=token_path,
        tailnet_trust_enabled=True,
    )
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
        "Tailscale CGNAT IP (100.64.0.5) should bypass credential check "
        "when tailnet_trust_enabled=True; "
        f"got {resp2.status_code} {resp2.text}"
    )


def test_cgnat_trust_disabled_returns_401_without_credentials(
    api_config, token_path: Path,
) -> None:
    """Pin the default mode: CGNAT trust is OFF in ``create_app()``.

    Codex round-2 blocker: an operator who runs
    ``pm serve --host 0.0.0.0 --allow-remote`` should get strict auth
    on every request even if the source IP lands in 100.64.0.0/10
    (some ISP CGNATs use RFC 6598 shared space). The default
    ``create_app`` build — and any ``pm serve`` invocation that didn't
    actually bind to a verified Tailscale IPv4 — must keep requiring
    a bearer or cookie.
    """
    from pollypm.web_api import create_app

    app = create_app(config=api_config, token_path=token_path)
    # Defensive: confirm the keyword defaulted to False (no implicit
    # tailnet trust). If someone flips the default we want this test
    # to fail loud.

    async def _probe() -> httpx.Response:
        transport = httpx.ASGITransport(app=app, client=("100.64.0.5", 12345))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver",
        ) as ac:
            return await ac.get("/api/v1/projects")

    resp = asyncio.run(_probe())
    assert resp.status_code == 401, (
        "Default create_app() must NOT trust CGNAT peers without creds; "
        f"got {resp.status_code} {resp.text}"
    )
    body = resp.json()
    assert body["error"]["code"] == "unauthorized"


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


# -------- Round-5: detect_tailscale_ip output validation ----------------
#
# Codex round-5 blocker: the detector previously returned the first
# non-empty stdout token from ``tailscale ip -4`` without validating
# either parseability or membership in the Tailscale CGNAT range
# (100.64.0.0/10). The serve path enables ``tailnet_trust=True``
# whenever this function returns a non-``None`` value, so leaking a
# malformed token (e.g. "not-an-ip") or a parseable-but-non-tailnet
# address would silently grant credential-free CGNAT trust to a peer
# we could not have verified came in on tailscale0. Lock the validator
# contract to (parseable IPv4) AND (inside CGNAT range).


def _patch_tailscale_stdout(stdout: str):
    """Build the (which + subprocess.run) double-mock the tests need."""

    class _FakeResult:
        def __init__(self) -> None:
            self.returncode = 0
            self.stdout = stdout

    def _fake_run(*_args, **_kwargs):
        return _FakeResult()

    return patch(
        "pollypm.cli_features.web_api.shutil.which",
        return_value="/usr/bin/tailscale",
    ), patch(
        "pollypm.cli_features.web_api.subprocess.run",
        side_effect=_fake_run,
    )


def test_detect_tailscale_ip_rejects_malformed_output() -> None:
    """Garbage on stdout → ``None`` (not the raw token)."""
    which_p, run_p = _patch_tailscale_stdout("not-an-ip\n")
    with which_p, run_p:
        assert detect_tailscale_ip() is None


def test_detect_tailscale_ip_rejects_non_cgnat_ipv4() -> None:
    """Parseable IPv4 outside 100.64.0.0/10 → ``None``.

    A LAN router that happens to be reachable as 192.168.1.1 must NOT
    be treated as a verified Tailscale peer; the serve path keys
    ``tailnet_trust=True`` off this function's truthiness.
    """
    which_p, run_p = _patch_tailscale_stdout("192.168.1.1\n")
    with which_p, run_p:
        assert detect_tailscale_ip() is None


def test_detect_tailscale_ip_rejects_public_ipv4() -> None:
    """A public IPv4 (e.g. 8.8.8.8) must also be refused."""
    which_p, run_p = _patch_tailscale_stdout("8.8.8.8\n")
    with which_p, run_p:
        assert detect_tailscale_ip() is None


def test_detect_tailscale_ip_rejects_ipv6_output() -> None:
    """IPv6 token (CGNAT is IPv4-only) → ``None``."""
    which_p, run_p = _patch_tailscale_stdout("fd7a:115c:a1e0::1\n")
    with which_p, run_p:
        assert detect_tailscale_ip() is None


def test_detect_tailscale_ip_accepts_valid_cgnat_ipv4() -> None:
    """A parseable IPv4 inside 100.64.0.0/10 → the address string."""
    which_p, run_p = _patch_tailscale_stdout("100.64.0.5\n")
    with which_p, run_p:
        assert detect_tailscale_ip() == "100.64.0.5"


def test_detect_tailscale_ip_accepts_upper_cgnat_boundary() -> None:
    """Boundary check: 100.127.255.254 is still inside 100.64.0.0/10."""
    which_p, run_p = _patch_tailscale_stdout("100.127.255.254\n")
    with which_p, run_p:
        assert detect_tailscale_ip() == "100.127.255.254"


def test_detect_tailscale_ip_rejects_just_outside_cgnat() -> None:
    """100.128.0.0 is one bit outside 100.64.0.0/10 → ``None``."""
    which_p, run_p = _patch_tailscale_stdout("100.128.0.1\n")
    with which_p, run_p:
        assert detect_tailscale_ip() is None


def test_detect_tailscale_ip_handles_empty_output() -> None:
    """Empty stdout → ``None`` (no token to validate)."""
    which_p, run_p = _patch_tailscale_stdout("")
    with which_p, run_p:
        assert detect_tailscale_ip() is None


def test_detect_tailscale_ip_handles_whitespace_only_output() -> None:
    """Whitespace-only stdout → ``None`` (no non-empty token)."""
    which_p, run_p = _patch_tailscale_stdout("\n   \n\t\n")
    with which_p, run_p:
        assert detect_tailscale_ip() is None


def test_pm_serve_does_not_trust_explicit_host_matching_garbage(
    api_config, token_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit ``--host not-an-ip`` cannot inherit tailnet trust.

    Companion to the detector tests: even if a future detector
    regression returned an unvalidated token, the validated detector
    contract guarantees ``detected is None`` whenever the candidate is
    not a verified CGNAT IPv4, so the explicit-host equality branch
    (``cli_features/web_api.py:245-248``) cannot flip
    ``tailnet_trust_enabled`` for a non-CGNAT host.
    """
    # detect_result=None simulates the post-validation detector
    # rejecting an invalid `tailscale ip -4` output.
    result, captured = _run_serve_command(
        api_config,
        token_path,
        monkeypatch,
        detect_result=None,
        extra_args=["--host", "192.168.1.1", "--allow-remote"],
    )
    assert result.exit_code == 0, result.output
    create_app_kwargs = captured["create_app_kwargs"]
    assert create_app_kwargs["tailnet_trust_enabled"] is False, (
        "Non-CGNAT explicit host must never enable tailnet trust; "
        f"got create_app kwargs: {create_app_kwargs}"
    )


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

    # Intercept ``create_app`` so the test can assert exactly which
    # kwargs (notably ``tailnet_trust_enabled``) flowed through. The
    # import inside the command body resolves the name on the
    # ``pollypm.web_api`` package, so patch it there.
    import pollypm.web_api as web_api_pkg

    real_create_app = web_api_pkg.create_app

    def _capturing_create_app(**kwargs):
        captured["create_app_kwargs"] = dict(kwargs)
        return real_create_app(**kwargs)

    monkeypatch.setattr(web_api_pkg, "create_app", _capturing_create_app)

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


# -------- Round-2: tailnet trust ↔ bind-mode wiring ---------------------


def test_pm_serve_disables_tailnet_trust_on_explicit_host_override(
    api_config, token_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit ``--host 0.0.0.0 --allow-remote`` keeps tailnet trust OFF.

    Round-2 Codex blocker: even when Tailscale IS detected, an operator
    who deliberately overrode the bind to a non-tailnet interface
    (e.g. 0.0.0.0 for a reverse proxy front-end) must NOT have the
    auth dependency hand out credential-free access to peers whose
    source IP lives in 100.64.0.0/10. Some ISP CGNATs use RFC 6598
    shared address space; trusting it on the public interface is the
    exact attack the round-2 review flagged.
    """
    result, captured = _run_serve_command(
        api_config,
        token_path,
        monkeypatch,
        detect_result="100.64.0.5",  # Tailscale IS up
        extra_args=["--host", "0.0.0.0", "--allow-remote"],
    )
    assert result.exit_code == 0, result.output
    assert captured["kwargs"]["host"] == "0.0.0.0"
    create_app_kwargs = captured["create_app_kwargs"]
    assert create_app_kwargs["tailnet_trust_enabled"] is False, (
        "Explicit --host override must NOT inherit tailnet trust even "
        "when detect_tailscale_ip succeeds; "
        f"got create_app kwargs: {create_app_kwargs}"
    )


def test_pm_serve_enables_tailnet_trust_on_auto_tailscale_bind(
    api_config, token_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default ``pm serve`` + detected Tailscale → tailnet_trust_enabled=True.

    Companion to the override test above: the auto-detect happy path
    is the ONLY mode that opts into credential-free CGNAT access.
    """
    result, captured = _run_serve_command(
        api_config,
        token_path,
        monkeypatch,
        detect_result="100.64.0.5",
    )
    assert result.exit_code == 0, result.output
    create_app_kwargs = captured["create_app_kwargs"]
    assert create_app_kwargs["tailnet_trust_enabled"] is True, (
        f"auto-tailscale bind should enable tailnet trust; got {create_app_kwargs}"
    )


def test_pm_serve_disables_tailnet_trust_on_loopback_fallback(
    api_config, token_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loopback fallback (no Tailscale detected) keeps tailnet trust OFF.

    Defensive: a future change that flipped the default to True would
    silently regress the round-2 invariant; pin it.
    """
    result, captured = _run_serve_command(
        api_config,
        token_path,
        monkeypatch,
        detect_result=None,
    )
    assert result.exit_code == 0, result.output
    create_app_kwargs = captured["create_app_kwargs"]
    assert create_app_kwargs["tailnet_trust_enabled"] is False, (
        f"loopback fallback should NOT enable tailnet trust; got {create_app_kwargs}"
    )


def test_pm_serve_with_explicit_host_skips_tailscale_detection(
    api_config, token_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit ``--host`` must skip ``detect_tailscale_ip`` entirely.

    Round-9 Codex blocker on #2065: the ``--host`` help text promises
    "Passing this flag skips Tailscale detection entirely", but the
    earlier round-5 implementation still re-called
    ``detect_tailscale_ip()`` in the explicit-host branch to
    opportunistically enable ``tailnet_trust_enabled`` when the typed
    host matched the detected IPv4. Help/code drift like that is the
    exact contract bug Codex flagged.

    This test wires ``detect_tailscale_ip`` to raise; if the explicit
    branch ever re-introduces the call, the ``RuntimeError`` propagates
    and the CLI exits non-zero, tripping this assertion.
    """
    import typer
    import uvicorn
    from typer.testing import CliRunner

    from pollypm.cli_features.web_api import register_web_api_commands

    root = typer.Typer()
    register_web_api_commands(root)

    captured: dict[str, object] = {}

    def _fake_uvicorn_run(*args, **kwargs):
        captured["kwargs"] = kwargs

    def _exploding_detect():
        raise RuntimeError(
            "detect_tailscale_ip must not be called in the explicit "
            "--host branch (round-9 #2065)."
        )

    monkeypatch.setattr(uvicorn, "run", _fake_uvicorn_run)
    monkeypatch.setattr(
        "pollypm.cli_features.web_api.detect_tailscale_ip",
        _exploding_detect,
    )
    monkeypatch.setattr(
        "pollypm.cli_features.web_api.load_config",
        lambda _path: api_config,
    )
    monkeypatch.setattr(
        "pollypm.web_api.ensure_token",
        lambda _path: ("test-token", False),
    )

    import pollypm.web_api as web_api_pkg

    real_create_app = web_api_pkg.create_app

    def _capturing_create_app(**kwargs):
        captured["create_app_kwargs"] = dict(kwargs)
        return real_create_app(**kwargs)

    monkeypatch.setattr(web_api_pkg, "create_app", _capturing_create_app)

    runner = CliRunner()
    result = runner.invoke(
        root,
        ["serve", "--token-path", str(token_path), "--host", "127.0.0.1"],
    )
    assert result.exit_code == 0, (
        f"detect_tailscale_ip was called in the explicit --host branch — "
        f"help-text/code drift regressed (round-9 #2065). Output: "
        f"{result.output}"
    )
    assert captured["kwargs"]["host"] == "127.0.0.1"
    # And the explicit branch never opts into tailnet trust, even for
    # a host that happens to look loopback.
    assert captured["create_app_kwargs"]["tailnet_trust_enabled"] is False


def test_warning_does_not_falsely_fire_on_explicit_host_with_tailscale_flag(
    api_config, token_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-13 #2065: ``--host X --tailscale`` must NOT lie about detection.

    The earlier warning gate fired whenever ``tailscale_ip is None`` and
    ``--tailscale`` was passed. But the explicit-host branch (round-9)
    never calls ``detect_tailscale_ip()`` — it just leaves
    ``tailscale_ip`` at None. So the warning's "Falling back to
    loopback-only" tail was doubly wrong: detection never ran, and the
    server is honoring the explicit host, not falling back.

    Drive the CLI with ``--host 127.0.0.1 --tailscale`` and confirm the
    misleading "Falling back to loopback-only" text never appears.
    """
    result, _captured = _run_serve_command(
        api_config,
        token_path,
        monkeypatch,
        # detect_result is irrelevant here because the explicit-host
        # branch must skip detect_tailscale_ip entirely (see round-9
        # test above). Pass None so a regression that re-introduced the
        # detect call would still trip the old warning gate.
        detect_result=None,
        extra_args=["--host", "127.0.0.1", "--tailscale"],
    )
    assert result.exit_code == 0, result.output
    assert "Falling back to loopback-only" not in result.output, (
        "Round-13 #2065: --host explicit + --tailscale must not emit "
        "the loopback-fallback warning — detection was never attempted "
        f"and the bind honored the explicit host. Got: {result.output!r}"
    )
    assert "`tailscale ip -4` returned no IP" not in result.output, (
        "The detection-failure warning fired even though "
        "detect_tailscale_ip was never called (explicit --host branch)."
    )


# -------- P1: mobile CSS -----------------------------------------------


def test_styles_have_mobile_media_query(client: TestClient) -> None:
    """``styles.css`` ships a mobile/tablet collapse breakpoint."""
    body = client.get("/ui/styles.css").text
    assert "@media (max-width: 768px)" in body, (
        "expected mobile/tablet media query for phone Tailscale users"
    )


# -------- Section 9: surface rail filter --------------------------------


def test_ui_surface_filter_input_rendered(client: TestClient) -> None:
    """The web UI renders a search input above the surface rail."""
    body = client.get("/ui/").text
    assert 'id="surface-filter"' in body
    assert 'type="search"' in body
    assert 'aria-label="Filter surfaces"' in body


def test_ui_app_js_filters_surfaces_case_insensitive(client: TestClient) -> None:
    """Surface filtering is client-side and case-insensitive."""
    body = client.get("/ui/app.js").text
    assert "state.surfaceFilter" in body
    assert ".toLowerCase().includes(filter)" in body
    assert 'addEventListener("input"' in body


# -------- Round-4: renderDashboard ↔ real /dashboard schema -------------


def test_ui_app_js_reads_real_dashboard_fields(client: TestClient) -> None:
    """``app.js`` reads the real ``DashboardResponse`` schema.

    Round-3 Codex blocker: the v0 right rail was looking for top-level
    counters like ``attention_count`` / ``workers.active`` that don't
    exist on ``GET /api/v1/dashboard``. The real envelope groups
    operator counters under ``rollups`` (see
    ``src/pollypm/web_api/routes/dashboard.py:DashboardRollups``). Pin
    the rename so a future refactor that drops the ``rollups.`` prefix
    or stops reading ``daemon_status`` fails this assertion before it
    ships an empty rail again.
    """
    body = client.get("/ui/app.js").text
    assert "rollups." in body, (
        "app.js must read counters off the ``rollups`` envelope, not "
        "from phantom top-level fields"
    )
    real_fields = [
        "open_inbox_count",
        "pending_plan_reviews",
        "alert_count",
        "daemon_status",
        "sweep_count_24h",
        "message_count_24h",
        "active_sessions",
        "tracked_count",
    ]
    hits = [name for name in real_fields if name in body]
    assert len(hits) >= 3, (
        f"app.js should reference at least 3 real DashboardResponse "
        f"fields; only found: {hits}"
    )


def test_ui_app_js_no_phantom_dashboard_fields(client: TestClient) -> None:
    """``app.js`` must not look for the pre-round-4 phantom field names.

    The fallback ``Object.keys(data).slice(0, 6)`` branch in the old
    ``renderDashboard`` rendered top-level envelope keys like
    ``"projects"`` and ``"rollups"`` as ``"N keys"`` cards whenever the
    candidate paths missed — which they always did against the real
    API. Pin both the phantom candidate paths AND the dead fallback
    branch so a regression re-introducing either fails loudly.
    """
    body = client.get("/ui/app.js").text
    phantom_paths = [
        "attention_count",
        "blocked_count",
        '"workers", "active"',
        '"tasks", "open"',
        '"projects", "tracked"',
    ]
    leaks = [name for name in phantom_paths if name in body]
    assert not leaks, (
        f"app.js still references phantom dashboard fields: {leaks}. "
        f"Real schema is documented on DashboardRollups in "
        f"src/pollypm/web_api/routes/dashboard.py."
    )
    # The old fallback ("N keys" cards from arbitrary top-level keys)
    # was the symptom Codex flagged in round 3. Make sure the rewrite
    # removed it.
    assert "N keys" not in body
    assert "+ \" keys\"" not in body
    assert '" keys"' not in body, (
        "app.js still contains the dead ``N keys`` fallback that "
        "rendered envelope keys as cards when the candidate paths "
        "missed"
    )


def test_ui_app_js_renderdashboard_uses_buildcard_helper(
    client: TestClient,
) -> None:
    """The rewrite centralizes card creation in a single helper.

    Codex round-3 asked for the mapping to live in ``one small adapter
    in app.js``. Pin that structure so future edits don't fan out
    inline card-building logic across the function and re-grow the
    schema drift surface.
    """
    body = client.get("/ui/app.js").text
    assert "function buildCard(" in body, (
        "renderDashboard must route card construction through a single "
        "``buildCard`` helper so the schema mapping stays centralized"
    )
    # And ``scoped_fields`` is consumed so project-filtered counters
    # are visibly tagged ("(filtered)") rather than presented as
    # workspace-wide.
    assert "scoped_fields" in body
    assert "(filtered)" in body


def test_ui_app_js_has_surface_audit_panel_hooks(client: TestClient) -> None:
    """The selected-surface pane exposes the audit grep panel."""
    body = client.get("/ui/app.js").text
    for expected in (
        "/audit/grep",
        "AUDIT_LIMIT = 25",
        "renderAuditPanel",
        "loadAuditForSurface",
        "auditPatternForSurface",
        "audit-toggle",
    ):
        assert expected in body


# -------- Round-5: executable renderDashboard coverage ------------------
#
# Codex round-5 blocker: the prior three tests (static greps over
# ``/ui/app.js``) cannot catch a runtime mapping bug where the right
# field names appear in source but flow into the wrong card, or where a
# refactor silently breaks the rendered output. Feed a representative
# ``DashboardResponse`` payload through the REAL ``renderDashboard``
# under node, then assert visible labels + values land in the rendered
# DOM. This is the executable coverage Codex asked for.


def _node_render_dashboard(payload: dict) -> str:
    """Load ``app.js`` under node with a minimal DOM shim and render.

    Returns the ``innerHTML`` of the ``#dashboard-rollups`` container
    after ``window.PollyPM.renderDashboard(payload)`` executes. The DOM
    shim implements only what ``app.js`` touches at render time:
    ``document.getElementById``, ``document.createElement`` returning
    nodes with ``setAttribute`` / ``appendChild`` / ``className`` /
    ``textContent`` / ``innerHTML`` / ``querySelector``. We deliberately
    avoid pulling in ``jsdom`` to keep the dev-deps surface flat — this
    SPA is tiny enough that ~60 LOC of shim covers it.
    """
    node_bin = shutil.which("node")
    if node_bin is None:
        pytest.skip("node binary not available; cannot run executable JS harness")
    app_js_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "pollypm"
        / "web_api"
        / "ui"
        / "app.js"
    )
    assert app_js_path.is_file(), f"app.js missing at {app_js_path}"
    # When invoked as ``node -e <code> ARG1 ARG2``, ``process.argv`` is
    # ``[node, ARG1, ARG2]`` (no script slot for ``-e``), so the
    # app.js path lands at argv[1] and the JSON payload at argv[2].
    harness = r"""
const fs = require("fs");
const path = process.argv[1];
const payload = JSON.parse(process.argv[2]);

// ---- minimal DOM shim --------------------------------------------------
function makeNode(tag) {
  const node = {
    tagName: (tag || "div").toUpperCase(),
    children: [],
    attrs: {},
    className: "",
    textContent: "",
    style: {},
  };
  Object.defineProperty(node, "innerHTML", {
    get() {
      function render(n) {
        if (n.__text != null) {
          return String(n.__text)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;");
        }
        const tag = n.tagName.toLowerCase();
        const parts = [];
        for (const k of Object.keys(n.attrs)) {
          parts.push(k + '="' + n.attrs[k] + '"');
        }
        if (n.className) parts.push('class="' + n.className + '"');
        const open = parts.length
          ? "<" + tag + " " + parts.join(" ") + ">"
          : "<" + tag + ">";
        let inner = "";
        if (n.textContent && n.children.length === 0) {
          inner = String(n.textContent)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;");
        } else {
          inner = n.children.map(render).join("");
        }
        return open + inner + "</" + tag + ">";
      }
      return node.children.map(render).join("");
    },
    set(v) {
      if (v === "") node.children = [];
    },
  });
  node.setAttribute = function (k, v) { node.attrs[k] = v; };
  node.appendChild = function (child) { node.children.push(child); return child; };
  node.removeChild = function (child) {
    node.children = node.children.filter((c) => c !== child);
    return child;
  };
  node.querySelector = function () { return null; };
  node.addEventListener = function () {};
  return node;
}

const elements = {
  "dashboard-rollups": makeNode("div"),
  "surface-list": makeNode("ul"),
  "message-list": makeNode("div"),
  "send-input": makeNode("input"),
  "send-button": makeNode("button"),
  "send-form": makeNode("form"),
  "pane-title": makeNode("div"),
  "pane-meta": makeNode("div"),
  "conn-status": makeNode("div"),
  "layout": makeNode("div"),
};

global.document = {
  getElementById: (id) => elements[id] || null,
  createElement: (tag) => makeNode(tag),
  createTextNode: (text) => {
    const n = makeNode("span");
    n.__text = text;
    return n;
  },
  addEventListener: function () {},
  readyState: "complete",
  body: makeNode("body"),
};
global.window = {};
global.setInterval = function () { return 0; };
global.clearInterval = function () {};
global.setTimeout = function () { return 0; };
global.fetch = function () { return Promise.reject(new Error("no network")); };

// ---- load app.js IIFE --------------------------------------------------
const source = fs.readFileSync(path, "utf8");
// eslint-disable-next-line no-eval
eval(source);

if (!global.window.PollyPM || typeof global.window.PollyPM.renderDashboard !== "function") {
  console.error("PollyPM.renderDashboard not exposed");
  process.exit(2);
}
global.window.PollyPM.renderDashboard(payload);
process.stdout.write(elements["dashboard-rollups"].innerHTML);
"""
    proc = subprocess.run(
        [node_bin, "-e", harness, str(app_js_path), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"node harness failed (rc={proc.returncode}):\n"
            f"STDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
        )
    return proc.stdout


def test_render_dashboard_real_payload_executes() -> None:
    """Feed a representative DashboardResponse through ``renderDashboard``.

    Asserts the rendered DOM contains:
    - the inbox count + label
    - the plan-reviews count + label
    - the alert count + ``alerts`` label
    - the daemon ``up`` status + label
    - the activity sweeps/msgs label
    - the active-sessions count
    - the tracked-projects count

    Static greps could not catch (a) a refactor that routes
    ``rollups.open_inbox_count`` into the ``alerts`` card by mistake,
    or (b) a typo that drops the value from the rendered DOM while
    keeping the field name in source. This test fails on both.
    """
    payload = {
        "rollups": {
            "open_inbox_count": 7,
            "pending_plan_reviews": 2,
            "alert_count": 3,
            "sweep_count_24h": 12,
            "message_count_24h": 47,
            "tracked_count": 5,
        },
        "daemon_status": "up",
        "active_sessions": [
            {"session_name": "a"},
            {"session_name": "b"},
            {"session_name": "c"},
            {"session_name": "d"},
        ],
        "scoped_fields": [],
        "projects": [],
        "recent_messages": [],
    }
    rendered = _node_render_dashboard(payload)

    # Sanity: the container actually got cards.
    assert "rollup-card" in rendered, (
        f"renderDashboard produced no cards; output was:\n{rendered}"
    )

    # Each counter's label appears.
    for label in (
        "inbox",
        "plan reviews",
        "alerts",
        "activity (24h)",
        "daemon",
        "active sessions",
        "projects tracked",
    ):
        assert label in rendered, (
            f"expected card label {label!r} in rendered DOM; got:\n{rendered}"
        )

    # Values are present (these are the load-bearing assertions — a
    # mapping bug that routes the wrong field into the wrong card
    # would fail here even if the labels still grep-match).
    assert "7 items" in rendered, "inbox count missing/wrong"
    assert "2 waiting" in rendered, "plan-reviews count missing/wrong"
    assert ">3<" in rendered, "alert_count value 3 missing"
    assert "12 sweeps / 47 msgs" in rendered, "activity 24h composite missing"
    assert ">up<" in rendered, "daemon status 'up' missing"
    assert ">4<" in rendered, "active_sessions count (len=4) missing"
    assert ">5<" in rendered, "tracked_count value 5 missing"

    # Color-coding contract: daemon=up → rollup-working; alert_count>0
    # → rollup-blocked. Pin these too so a future restyle that drops
    # the class hooks fails loudly.
    assert "rollup-working" in rendered
    assert "rollup-blocked" in rendered


def test_render_dashboard_daemon_down_uses_blocked_class() -> None:
    """``daemon_status='down'`` flips the daemon card to the blocked class.

    Executable variant of the per-status branch in ``renderDashboard``
    (``app.js:358-367``). Pinning this catches a future swap of
    ``up``/``down`` or ``rollup-working``/``rollup-blocked``.
    """
    payload = {
        "rollups": {
            "open_inbox_count": 0,
            "pending_plan_reviews": 0,
            "alert_count": 0,
            "sweep_count_24h": 0,
            "message_count_24h": 0,
            "tracked_count": 0,
        },
        "daemon_status": "down",
        "active_sessions": [],
        "scoped_fields": [],
        "projects": [],
        "recent_messages": [],
    }
    rendered = _node_render_dashboard(payload)
    assert ">down<" in rendered
    # daemon card is the one with text "daemon" and class rollup-blocked
    assert "rollup-blocked" in rendered


def test_render_dashboard_scoped_fields_tag_filtered_cards() -> None:
    """``scoped_fields`` flags a card with ``(filtered)`` in its label.

    Executable variant of the ``scoped_fields`` path
    (``app.js:280-291``). Confirms the runtime mapping actually appends
    the tag rather than just containing the literal in source.
    """
    payload = {
        "rollups": {
            "open_inbox_count": 1,
            "pending_plan_reviews": 0,
            "alert_count": 0,
            "sweep_count_24h": 0,
            "message_count_24h": 0,
            "tracked_count": 0,
        },
        "daemon_status": "up",
        "active_sessions": [],
        "scoped_fields": ["rollups.open_inbox_count"],
        "projects": [],
        "recent_messages": [],
    }
    rendered = _node_render_dashboard(payload)
    assert "inbox (filtered)" in rendered, (
        f"scoped inbox card should carry the (filtered) tag; got:\n{rendered}"
    )
