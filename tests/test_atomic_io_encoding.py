"""Cycle 125 — atomic_io always writes UTF-8.

``atomic_write_json`` and ``atomic_write_text`` previously relied on
the system's locale-default encoding. On Windows CP-1252 or a
legacy Linux install, non-ASCII content (emojis in commit messages,
notify subjects, project names) would either raise UnicodeEncodeError
mid-write or get silently mangled. The downstream JSON readers all
read with the default encoding too, so a write/read cycle could
round-trip cleanly on POSIX UTF-8 but break on a different machine.

Pin UTF-8 explicitly.
"""

from __future__ import annotations

import json
import locale
from pathlib import Path

import pytest

from pollypm.atomic_io import atomic_write_json, atomic_write_text


_NON_ASCII = {
    "rocket": "🚀",
    "shrug": "🤷",
    "diamond": "◆",
    "japanese": "プロジェクト",
    "accents": "café",
}


def test_atomic_write_json_round_trips_emojis(tmp_path: Path) -> None:
    """Defended against mojibake: read back exactly what was written."""
    target = tmp_path / "data.json"
    atomic_write_json(target, _NON_ASCII)
    decoded = json.loads(target.read_bytes().decode("utf-8"))
    assert decoded == _NON_ASCII


def test_atomic_write_text_round_trips_emojis(tmp_path: Path) -> None:
    target = tmp_path / "data.txt"
    payload = "🚀 — café — プロジェクト\n"
    atomic_write_text(target, payload)
    assert target.read_bytes().decode("utf-8") == payload


def test_atomic_write_text_uses_utf8_for_non_ascii_payload(tmp_path: Path) -> None:
    """``atomic_write_text`` carries raw text — non-ASCII bytes must
    land as UTF-8 so downstream readers (which decode UTF-8 by
    contract) can round-trip them. With the default locale opener and
    a non-UTF-8 system locale, this would have raised UnicodeEncodeError
    or written mojibake bytes that the next read would mangle."""
    target = tmp_path / "data.txt"
    atomic_write_text(target, "🚀 café プロジェクト")
    raw = target.read_bytes()
    # 🚀 → 0xF0 0x9F 0x9A 0x80
    assert b"\xf0\x9f\x9a\x80" in raw
    # café → c-a-f + 0xC3 0xA9
    assert b"caf\xc3\xa9" in raw
