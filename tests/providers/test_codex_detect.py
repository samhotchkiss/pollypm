"""Tests for ``pollypm.providers.codex.detect`` (Phase C of #397).

Run with::

    HOME=/tmp/pytest-providers-codex uv run --with pytest \\
        pytest tests/providers/test_codex_detect.py -x

These tests exercise the email-detection helpers directly — email from
``auth.json`` (the primary source), email from the tmux pane
scrollback (the onboarding fast-path), and the boolean login-state
wrapper that callers use when they only need a presence check.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from pollypm.providers.codex.detect import (
    detect_codex_email,
    detect_email_from_pane,
    detect_logged_in,
)


def _fake_id_token(claims: dict[str, object]) -> str:
    """Build a well-formed JWT (unsigned) carrying ``claims`` as the payload.

    The detect helpers only read the payload, so a three-segment token
    with any header/signature bytes is sufficient.
    """

    def _b64(payload: dict[str, object] | str) -> str:
        if isinstance(payload, dict):
            data = json.dumps(payload).encode("utf-8")
        else:
            data = payload.encode("utf-8")
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    header = _b64({"alg": "none", "typ": "JWT"})
    body = _b64(claims)
    signature = _b64("signature")
    return f"{header}.{body}.{signature}"


def _write_auth_json(home: Path, id_token: str) -> None:
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    (codex_dir / "auth.json").write_text(
        json.dumps({"tokens": {"id_token": id_token}})
    )


def test_detect_codex_email_returns_lowercased_email(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_auth_json(home, _fake_id_token({"email": "User@Example.COM"}))

    assert detect_codex_email(home) == "user@example.com"


def test_detect_codex_email_returns_none_when_auth_missing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    # No auth.json at all.

    assert detect_codex_email(home) is None


def test_detect_codex_email_returns_none_for_malformed_token(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_auth_json(home, "not-a-jwt")

    assert detect_codex_email(home) is None


def test_detect_codex_email_returns_none_when_email_claim_absent(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _write_auth_json(home, _fake_id_token({"sub": "abc123"}))

    assert detect_codex_email(home) is None


def test_detect_logged_in_handles_none_home() -> None:
    """Matches ``pollypm.accounts._account_logged_in``: None -> False."""
    assert detect_logged_in(None) is False


def test_detect_logged_in_returns_true_when_auth_present(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_auth_json(home, _fake_id_token({"email": "s@example.com"}))

    assert detect_logged_in(home) is True


def test_detect_logged_in_returns_false_when_auth_missing(tmp_path: Path) -> None:
    home = tmp_path / "home"

    assert detect_logged_in(home) is False


def test_detect_email_from_pane_parses_codex_account_line() -> None:
    pane = "\n".join(
        [
            "openai codex ready",
            "Account: s@example.com",
            "> ",
        ]
    )

    assert detect_email_from_pane(pane) == "s@example.com"


def test_detect_email_from_pane_lowercases_match() -> None:
    pane = "Account: Sam@Example.COM"

    assert detect_email_from_pane(pane) == "sam@example.com"


def test_detect_email_from_pane_returns_none_when_marker_absent() -> None:
    pane = "openai codex ready\n100% left\n"

    assert detect_email_from_pane(pane) is None
