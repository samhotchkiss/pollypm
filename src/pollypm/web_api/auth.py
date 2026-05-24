"""Bearer-token auth dependency.

Per `docs/web-api-spec.md` §3, every request except ``GET
/api/v1/health`` carries an ``Authorization: Bearer <token>`` header
that must match the contents of the token file
(``~/.pollypm/api-token`` by default).

The dependency reads the token fresh from disk on each request so a
``pm api regen-token`` rotation invalidates outstanding bearer tokens
and browser sessions immediately — there's no in-memory cache to flush.
Personal-use volume makes that trivially cheap (one ``open + read`` per
request).

V0 web UI (Phase 7) extends the auth dependency with two additional
acceptance modes for browser-driven sessions:

- ``pollypm-session`` cookie — set by ``GET /ui/`` as a per-browser
  opaque session value signed with the current on-disk token, so the
  browser never has to paste / type the bearer token. Rotation
  invalidates the signature.
- Tailscale CGNAT trust — request ``client.host`` inside the Tailscale
  CGNAT range (``100.64.0.0/10``) is allowed without either credential,
  **but only when the app was built with ``tailnet_trust_enabled=True``**.
  ``pm serve`` only flips that flag when it actually bound to a verified
  Tailscale IPv4 (see ``cli_features/web_api.py``). An operator who
  runs ``pm serve --host 0.0.0.0 --allow-remote`` gets ``False`` even if
  Tailscale is running — RFC 6598 shared address space is used by some
  CGNAT-enabled ISPs, so an unauthenticated 100.64.x.y peer arriving on
  a non-tailnet interface must NOT be trusted.

Loopback (``127.0.0.1``, ``::1``) is not auto-trusted by the API auth
dependency itself — a local process without the bearer token still
receives 401 from ``/api/v1/...``. However, ``GET /ui/`` deliberately
mints a signed ``pollypm-session`` cookie for any loopback caller (and
for tailnet callers when the flag above is on) as a convenience for the
local-operator workflow: a browser on the same machine shouldn't have to
paste a bearer to view the cockpit. See
``docs/web-ui-2065-security-spec.md`` decision **d-ii** for the
signed-off trade-off — loopback-cookie-mint is an explicit local-
operator bypass, not an oversight.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets
import time
from pathlib import Path

from fastapi import Cookie, Header, Query, Request

from pollypm.web_api.errors import invalid_token, unauthorized
from pollypm.web_api.token import load_token


# Tailscale's CGNAT range — every node on a tailnet gets a 100.64.0.0/10
# address. We trust requests originating from this range as already
# authenticated by Tailscale itself (the operator opted into Tailscale
# auth when they joined the tailnet).
_TAILSCALE_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")

# Cookie name used by ``GET /ui/`` to seed the browser session.
SESSION_COOKIE_NAME = "pollypm-session"
SESSION_ISSUED_COOKIE_NAME = "pollypm-session-issued-at"
SESSION_COOKIE_TTL_SECONDS = 60 * 60 * 24 * 7
_SESSION_COOKIE_VERSION = "v1"
_SESSION_COOKIE_MAX_CLOCK_SKEW_SECONDS = 300


def is_tailscale_ip(host: str | None) -> bool:
    """Return True iff ``host`` parses as an IP inside the CGNAT range.

    Anything non-IP (hostnames, ``None``, malformed values) returns
    ``False`` so we never accidentally trust a string that looks
    plausible but isn't actually a tailnet address.

    .. note::
       This is a **sanity filter, not the primary trust boundary**. The
       100.64.0.0/10 range is RFC 6598 shared address space; Tailscale
       uses it but so does CGNAT-enabled ISP infrastructure. The real
       tailnet-trust enforcement is the OS-level interface binding done
       by ``pm serve`` (see ``cli_features/web_api.py``): uvicorn binds
       to the detected Tailscale IPv4 only, so packets arriving on any
       other interface are dropped by the kernel before reaching this
       check. This helper exists for defense-in-depth: if a future
       deploy widens the bind by accident (or the operator runs the
       server behind a reverse proxy that doesn't preserve the peer
       address), the CGNAT filter still keeps random LAN devices out.
    """
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr in _TAILSCALE_CGNAT_NET


def _extract_token(header_value: str | None) -> str | None:
    """Pull the token out of a ``Bearer <token>`` Authorization header.

    Returns ``None`` for any malformed header so the dependency can
    raise the right typed error code (``unauthorized`` for missing,
    ``invalid_token`` for wrong / malformed).
    """
    if not header_value:
        return None
    parts = header_value.strip().split(None, 1)
    if len(parts) != 2:
        return None
    scheme, value = parts
    if scheme.lower() != "bearer":
        return None
    value = value.strip()
    return value or None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _session_cookie_signature(api_token: str, payload: str) -> str:
    return _b64url(
        hmac.new(
            api_token.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    )


def mint_session_cookie(
    api_token: str,
    *,
    issued_at: int | None = None,
    nonce: str | None = None,
) -> str:
    """Return a per-browser opaque session cookie value.

    The raw bearer token never goes into the browser cookie. Instead we
    sign ``version.issued_at.nonce`` with the current API token as the
    HMAC key. Since validation reloads the API token from disk on every
    request, ``pm api regen-token`` invalidates existing browser
    sessions without a server-side registry.
    """
    issued = int(time.time() if issued_at is None else issued_at)
    session_nonce = nonce or secrets.token_urlsafe(32)
    payload = f"{_SESSION_COOKIE_VERSION}.{issued}.{session_nonce}"
    signature = _session_cookie_signature(api_token, payload)
    return f"{payload}.{signature}"


def is_valid_session_cookie(
    cookie_value: str,
    api_token: str,
    *,
    now: int | None = None,
) -> bool:
    """Validate a ``pollypm-session`` cookie against the current API token."""
    parts = cookie_value.split(".")
    if len(parts) != 4:
        return False
    version, issued_raw, nonce, signature = parts
    if version != _SESSION_COOKIE_VERSION or not nonce or not signature:
        return False
    try:
        issued_at = int(issued_raw)
    except ValueError:
        return False

    current = int(time.time() if now is None else now)
    if issued_at > current + _SESSION_COOKIE_MAX_CLOCK_SKEW_SECONDS:
        return False
    if current - issued_at > SESSION_COOKIE_TTL_SECONDS:
        return False

    payload = ".".join(parts[:3])
    expected = _session_cookie_signature(api_token, payload)
    from secrets import compare_digest

    return compare_digest(signature, expected)


def _invalid_session_cookie():
    return invalid_token(
        "Session cookie is invalid or expired. Reload /ui/ to refresh it."
    )


def make_bearer_auth_dependency(
    token_path: Path | None = None,
    *,
    tailnet_trust_enabled: bool = False,
):
    """Return a FastAPI dependency that enforces bearer-token auth.

    Constructed at app-creation time so tests can swap in a tmp token
    path without monkeypatching module-level state.

    Accepts three credential modes (in order):
    1. ``Authorization: Bearer <token>`` header.
    2. Signed ``pollypm-session`` cookie (web UI).
    3. Tailscale CGNAT client IP (no credential needed) — **only when
       ``tailnet_trust_enabled`` is True**.

    The tailnet-trust flag is opt-in so a server that did NOT bind to
    a verified Tailscale interface (e.g. ``pm serve --host 0.0.0.0
    --allow-remote``) never grants credential-free access to peers
    whose source IP happens to land in RFC 6598 shared address space.
    Some ISPs use 100.64.0.0/10 for CGNAT; without the gate, those
    peers would walk past auth.

    Wrong tokens / cookies still produce ``invalid_token``; missing
    everything (and not from a trusted tailnet IP) produces
    ``unauthorized``.
    """

    def _dependency(
        request: Request,
        authorization: str | None = Header(default=None),
        session_cookie: str | None = Cookie(
            default=None, alias=SESSION_COOKIE_NAME,
        ),
    ) -> str:
        # Determine which credential the caller supplied. Bearer tokens
        # compare directly against the on-disk token; session cookies are
        # signed per-browser values validated with that token as the key.
        # Only the Tailscale path skips token loading entirely.
        bearer_token = _extract_token(authorization)
        cookie_token = session_cookie.strip() if session_cookie else None

        if bearer_token is None and not cookie_token:
            # No credential at all — check Tailscale fallback before
            # rejecting. ``request.client`` is ``None`` for ASGI
            # transports without a peer (rare; treat as unauthenticated).
            client_host = request.client.host if request.client else None
            if tailnet_trust_enabled and is_tailscale_ip(client_host):
                return f"tailscale:{client_host}"
            raise unauthorized()

        expected = load_token(token_path)
        if expected is None:
            # Token file doesn't exist — first-run footgun. Treat as
            # unauthorized (the operator hasn't set up the API yet) so
            # the response matches ``code=unauthorized``, not ``500``.
            raise unauthorized(
                "Bearer token required; run `pm serve` once or "
                "`pm api regen-token` to provision."
            )
        # Constant-time comparison to avoid leaking the prefix length
        # via timing. Personal-use scope means this is overkill, but
        # it's free.
        from secrets import compare_digest

        if bearer_token is not None:
            if not compare_digest(bearer_token, expected):
                raise invalid_token()
            return bearer_token

        if cookie_token and is_valid_session_cookie(cookie_token, expected):
            return cookie_token

        if cookie_token:
            # A stale / tampered / legacy raw-token cookie deserves a
            # clearer hint than the raw ``invalid_token`` message.
            raise _invalid_session_cookie()

        raise unauthorized()

    return _dependency


def make_sse_auth_dependency(
    token_path: Path | None = None,
    *,
    tailnet_trust_enabled: bool = False,
):
    """Return a FastAPI dependency for the SSE stream specifically.

    The browser ``EventSource`` API cannot send custom headers, so the
    spec (§4) lets clients pass the bearer token via ``?token=`` for
    the SSE endpoint only. The header form is still accepted (and
    preferred for non-browser clients); the query-string fallback is
    a SSE-only escape hatch — do NOT reuse this for read or write
    endpoints, since query strings end up in proxy / browser-history
    logs.

    Also honors the v0 web UI session cookie and (when
    ``tailnet_trust_enabled`` is True) the Tailscale CGNAT trust,
    matching the behavior of :func:`make_bearer_auth_dependency`, so
    the SPA can subscribe to ``/events`` without paste-box gymnastics.
    """

    def _dependency(
        request: Request,
        authorization: str | None = Header(default=None),
        token: str | None = Query(default=None, description=(
            "Bearer token, SSE-only fallback for browser EventSource which "
            "cannot send Authorization headers. Prefer the Authorization "
            "header for every other endpoint."
        )),
        session_cookie: str | None = Cookie(
            default=None, alias=SESSION_COOKIE_NAME,
        ),
    ) -> str:
        provided = _extract_token(authorization)
        from_cookie = False
        if provided is None:
            # Fall back to the query-string escape hatch.
            if token:
                provided = token.strip() or None
        if provided is None and session_cookie:
            provided = session_cookie.strip() or None
            from_cookie = provided is not None
        if provided is None:
            client_host = request.client.host if request.client else None
            if tailnet_trust_enabled and is_tailscale_ip(client_host):
                return f"tailscale:{client_host}"
            raise unauthorized()
        expected = load_token(token_path)
        if expected is None:
            raise unauthorized(
                "Bearer token required; run `pm serve` once or "
                "`pm api regen-token` to provision."
            )
        from secrets import compare_digest

        if from_cookie:
            if is_valid_session_cookie(provided, expected):
                return provided
            raise _invalid_session_cookie()

        if not compare_digest(provided, expected):
            raise invalid_token()
        return provided

    return _dependency


__all__ = [
    "SESSION_COOKIE_NAME",
    "SESSION_COOKIE_TTL_SECONDS",
    "_extract_token",
    "is_tailscale_ip",
    "is_valid_session_cookie",
    "make_bearer_auth_dependency",
    "make_sse_auth_dependency",
    "mint_session_cookie",
]
