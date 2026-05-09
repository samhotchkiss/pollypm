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
