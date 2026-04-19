"""Codex email / login detection — Phase C of #397.

Codex stores its OAuth tokens in ``<CODEX_HOME>/auth.json``. The
authenticated email is embedded in the ``id_token`` JWT, so detecting
login-state and detecting the user's email are the same operation.

Two public helpers:

* :func:`detect_codex_email` — read ``auth.json`` and return the email
  or ``None``.
* :func:`detect_logged_in` — convenience wrapper that returns a bool.

Also :func:`detect_email_from_pane` handles the onboarding case where
the tmux pane shows ``Account: <email>`` after Codex finishes its login
flow. Moved from ``onboarding._detect_email_from_pane`` (Codex branch
only — Claude parts live in Phase B's package).

Previously lived in ``pollypm.onboarding._detect_codex_email``. That
symbol is re-exported from ``pollypm.onboarding`` as a back-compat shim.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path


def _decode_jwt_payload(token: str) -> dict[str, object]:
    """Decode the middle segment of a JWT into a dict.

    Copied from ``onboarding._decode_jwt_payload`` (which remains as the
    canonical implementation for now). Duplicated here so the Codex
    provider package does not need to reach back into onboarding for a
    three-line helper.
    """
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def detect_codex_email(home: Path) -> str | None:
    """Return the authenticated Codex email, or ``None`` if unknown.

    Reads ``<home>/.codex/auth.json`` and extracts the ``email`` claim
    from the ``id_token`` JWT. Any parse failure, missing file, or
    malformed token is silently swallowed (returns ``None``) — the
    caller treats ``None`` as "not logged in" so we do not surface the
    distinction.
    """
    auth_path = home / ".codex" / "auth.json"
    if not auth_path.exists():
        return None
    try:
        data = json.loads(auth_path.read_text())
        id_token = data.get("tokens", {}).get("id_token")
        if not isinstance(id_token, str) or id_token.count(".") < 2:
            return None
        payload = _decode_jwt_payload(id_token)
        email = payload.get("email")
        return str(email).lower() if isinstance(email, str) and email else None
    except Exception:  # noqa: BLE001
        return None


def detect_logged_in(home: Path | None) -> bool:
    """Return True iff the Codex profile at ``home`` is authenticated.

    Null-safe: ``home is None`` returns False so callers can pass the
    ``AccountConfig.home`` field without a pre-check. Matches the
    contract of ``pollypm.accounts._account_logged_in`` for Codex.
    """
    if home is None:
        return False
    return detect_codex_email(home) is not None


def detect_email_from_pane(pane_text: str) -> str | None:
    """Extract the authenticated email from a Codex login-window pane.

    Codex prints ``Account: <email>`` once its OAuth callback resolves.
    We pull it straight out of the scrollback so onboarding can advance
    the moment login completes, without waiting for ``auth.json`` to
    hit disk. Returns ``None`` when the marker is not present.
    """
    match = re.search(
        r"Account:\s+([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})",
        pane_text,
    )
    if match:
        return match.group(1).lower()
    return None


__all__ = ["detect_codex_email", "detect_email_from_pane", "detect_logged_in"]
