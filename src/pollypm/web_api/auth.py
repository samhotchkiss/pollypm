"""Bearer-token auth dependency.

Per `docs/web-api-spec.md` §3, every request except ``GET
/api/v1/health`` carries an ``Authorization: Bearer <token>`` header
that must match the contents of the token file
(``~/.pollypm/api-token`` by default).

The dependency reads the token fresh from disk on each request so a
``pm api regen-token`` rotation invalidates outstanding sessions
immediately — there's no in-memory cache to flush. Personal-use volume
makes that trivially cheap (one ``open + read`` per request).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Header

from pollypm.web_api.errors import invalid_token, unauthorized
from pollypm.web_api.token import load_token


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


def make_bearer_auth_dependency(token_path: Path | None = None):
    """Return a FastAPI dependency that enforces bearer-token auth.

    Constructed at app-creation time so tests can swap in a tmp token
    path without monkeypatching module-level state.
    """

    def _dependency(authorization: str | None = Header(default=None)) -> str:
        provided = _extract_token(authorization)
        if provided is None:
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
            raise invalid_token()
        return provided

    return _dependency


__all__ = ["make_bearer_auth_dependency"]
