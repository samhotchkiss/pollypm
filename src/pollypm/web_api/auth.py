"""Bearer-token auth dependency.

Per `docs/web-api-spec.md` §3, every request except ``GET
/api/v1/health`` carries an ``Authorization: Bearer <token>`` header
that must match the contents of the token file
(``~/.pollypm/api-token`` by default).

The dependency reads the token fresh from disk on each request so a
``pm api regen-token`` rotation invalidates outstanding sessions
immediately — there's no in-memory cache to flush. Personal-use volume
makes that trivially cheap (one ``open + read`` per request).

V0 web UI (Phase 7) extends the auth dependency with two additional
acceptance modes for browser-driven sessions:

- ``pollypm-session`` cookie — set by ``GET /ui/`` from the on-disk
  token, so the browser never has to paste / type the token. Same
  comparison rules as the header (constant-time, rotation invalidates).
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
mints a ``pollypm-session`` cookie from the on-disk token for any
loopback caller (and for tailnet callers when the flag above is on) as
a convenience for the local-operator workflow: a browser on the same
machine shouldn't have to paste a bearer to view the cockpit. See
``docs/web-ui-2065-security-spec.md`` decision **d-ii** for the
signed-off trade-off — loopback-cookie-mint is an explicit local-
operator bypass, not an oversight.
"""

from __future__ import annotations

import ipaddress
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
    2. ``pollypm-session`` cookie (web UI).
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
        # Determine which credential the caller supplied. We compare
        # against the on-disk token for both header and cookie modes;
        # only the Tailscale path skips the comparison entirely.
        provided: str | None = _extract_token(authorization)
        from_cookie = False
        if provided is None and session_cookie:
            provided = session_cookie.strip() or None
            from_cookie = provided is not None

        if provided is None:
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

        if not compare_digest(provided, expected):
            # A stale cookie deserves a clearer hint than the raw
            # ``invalid_token`` message — the operator likely rotated
            # the token and the browser is still sending the old one.
            if from_cookie:
                raise invalid_token(
                    "Session cookie does not match the current token. "
                    "Reload /ui/ to refresh the cookie from disk."
                )
            raise invalid_token()
        return provided

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
        if provided is None:
            # Fall back to the query-string escape hatch.
            if token:
                provided = token.strip() or None
        if provided is None and session_cookie:
            provided = session_cookie.strip() or None
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

        if not compare_digest(provided, expected):
            raise invalid_token()
        return provided

    return _dependency


__all__ = [
    "SESSION_COOKIE_NAME",
    "_extract_token",
    "is_tailscale_ip",
    "make_bearer_auth_dependency",
    "make_sse_auth_dependency",
]
