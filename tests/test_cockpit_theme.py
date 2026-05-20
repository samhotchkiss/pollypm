"""Smoke tests for the semantic palette + glyph module.

Guards against the most likely regression: a future palette tweak that
silently breaks a glyph/color invariant that ``cockpit_rail_item`` (and
the rest of the cockpit) relies on.
"""

from __future__ import annotations

import re

import pytest

from pollypm.cockpit_theme import Glyph, State, hex_to_rgb


_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


@pytest.mark.parametrize(
    "name",
    [n for n in dir(State) if not n.startswith("_") and isinstance(getattr(State, n), str)],
)
def test_state_constants_are_hex_strings(name: str) -> None:
    value = getattr(State, name)
    assert _HEX_RE.match(value), f"State.{name} = {value!r} is not a #rrggbb hex string"


def test_state_blocked_matches_rail_palette_red() -> None:
    """Inbox plan-review row historically drifted to ``#ff6b5b`` while the
    rail used ``#ff5f6d``. We standardise on the rail's; lock that in.
    """
    assert State.BLOCKED == "#ff5f6d"


def test_glyph_attention_is_yellow_diamond() -> None:
    """Sanity-check the glyph vocabulary so a stray reshuffle doesn't
    silently swap meanings (e.g. ATTENTION → ▲ would break the rail).
    """
    assert Glyph.ATTENTION == "◆"
    assert Glyph.BLOCKED == "▲"
    assert Glyph.DECISION == "▶"
    assert Glyph.IDLE == "○"
    assert Glyph.LIVE == "•"
    assert Glyph.WAITING == "◇"
    assert Glyph.REVIEW == "◉"


def test_glyph_pulse_pair() -> None:
    assert Glyph.PULSE_ON == "♥"
    assert Glyph.PULSE_OFF == "♡"


def test_glyph_spinner_is_four_frame_arc() -> None:
    assert Glyph.SPINNER == ("◜", "◝", "◞", "◟")
    assert len(Glyph.SPINNER) == 4


def test_hex_to_rgb_round_trip() -> None:
    assert hex_to_rgb("#000000") == (0, 0, 0)
    assert hex_to_rgb("#ffffff") == (255, 255, 255)
    assert hex_to_rgb(State.WAITING) == (0xf0, 0xc4, 0x5a)


def test_hex_to_rgb_rejects_short_or_unprefixed() -> None:
    with pytest.raises(ValueError):
        hex_to_rgb("f0c45a")
    with pytest.raises(ValueError):
        hex_to_rgb("#fff")
