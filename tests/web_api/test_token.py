"""Token storage / rotation tests.

Covers the spec §3 contract:
- File at ``~/.pollypm/api-token`` (mode ``0600``).
- Generated on first ``pm serve`` startup if absent.
- 256-bit random, base64url-encoded (~43 chars).
- Rotation invalidates the previous value.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from pollypm.web_api.token import ensure_token, load_token, regenerate_token


def test_ensure_token_generates_when_missing(tmp_path: Path) -> None:
    path = tmp_path / "api-token"
    token, generated = ensure_token(path)
    assert generated is True
    assert path.exists()
    assert path.read_text(encoding="utf-8") == token
    # 256-bit url-safe ⇒ ~43 chars (no padding).
    assert 40 <= len(token) <= 44


def test_ensure_token_idempotent_when_present(tmp_path: Path) -> None:
    path = tmp_path / "api-token"
    first, generated_first = ensure_token(path)
    second, generated_second = ensure_token(path)
    assert generated_first is True
    assert generated_second is False
    assert first == second


def test_ensure_token_writes_mode_0600(tmp_path: Path) -> None:
    path = tmp_path / "api-token"
    ensure_token(path)
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_regenerate_token_replaces_previous(tmp_path: Path) -> None:
    path = tmp_path / "api-token"
    first, _ = ensure_token(path)
    rotated = regenerate_token(path)
    assert rotated != first
    assert load_token(path) == rotated


def test_load_token_returns_none_when_missing(tmp_path: Path) -> None:
    assert load_token(tmp_path / "absent") is None


def test_load_token_strips_whitespace(tmp_path: Path) -> None:
    path = tmp_path / "api-token"
    path.write_text("padded-token\n", encoding="utf-8")
    assert load_token(path) == "padded-token"


def test_pm_serve_does_not_print_existing_token(tmp_path: Path, capsys) -> None:
    """MED-3 regression: ``pm serve`` previously printed the token
    every startup, even when it already existed on disk. That leaks
    the secret into terminal scrollback / log files. Confirm only a
    location pointer is printed when ``ensure_token`` returns
    ``generated=False``.
    """
    from pollypm.cli_features.web_api import (
        _print_token_location_only,
        _print_token_once,
    )

    # Pre-generated token simulates the second-launch case.
    token, _ = ensure_token(tmp_path / "api-token")

    # First launch: token IS printed (the operator needs the value).
    _print_token_once(token, generated=True)
    first = capsys.readouterr()
    assert token in first.err
    assert "Token" in first.err

    # Subsequent launch path: only the location is surfaced. The
    # token VALUE must not appear in the printed banner.
    _print_token_location_only(str(tmp_path / "api-token"))
    second = capsys.readouterr()
    assert token not in second.err
    assert "api-token" in second.err
    assert "regen-token" in second.err
