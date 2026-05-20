"""Rail glyph cheatsheet completeness checks.

The cockpit's ``?`` help overlay surfaces a glyph cheatsheet from
``cockpit_palette._RAIL_GLYPH_HELP``. The audit on 2026-05-20 found
the cheatsheet undocumented half of the rail's vocabulary (◆, ▲, ▶,
◇, ◉, ✕, ⚙). These tests guard against regressing back to that.
"""

from __future__ import annotations

from pollypm.cockpit_palette import _RAIL_GLYPH_HELP


def _help_glyphs() -> set[str]:
    """Return the set of every individual char documented in the cheatsheet.

    Entries like ``"♥ / ♡"`` represent two glyphs sharing one help row,
    so split on ``/`` and strip surrounding whitespace.
    """
    glyphs: set[str] = set()
    for entry, _help in _RAIL_GLYPH_HELP:
        for chunk in entry.split("/"):
            chunk = chunk.strip()
            if chunk:
                glyphs.add(chunk)
    return glyphs


def test_cheatsheet_documents_core_glyph_vocabulary() -> None:
    """Every glyph the rail emits should appear in the help overlay.

    Pulled from ``cockpit_rail_item._indicator()`` and the headless
    rail's ``_indicator()`` — the full set of glyphs the operator can
    encounter and might ask "what does that mean?"
    """
    glyphs = _help_glyphs()
    required = {
        # Selection / nav
        "▌",
        # Pulse / session life
        "♥",
        "♡",
        # Work states
        "·",
        "✎",
        "⚠",
        "✕",
        # Project rollup
        "○",
        "•",
        "◆",
        "◇",
        "▲",
        "▶",
        "◉",
        # System / footer
        "⚙",
    }
    missing = required - glyphs
    assert not missing, f"cheatsheet missing {sorted(missing)}"


def test_cheatsheet_includes_spinner_frames() -> None:
    """Arc-spinner frames are animated, so document them as a group."""
    entries = [entry for entry, _ in _RAIL_GLYPH_HELP]
    assert any("◜◝◞◟" in e for e in entries), (
        f"spinner frames not documented; entries={entries!r}"
    )


def test_cheatsheet_entries_all_have_help_text() -> None:
    """Every cheatsheet row must have a non-empty help string."""
    for entry, help_text in _RAIL_GLYPH_HELP:
        assert help_text, f"empty help for entry {entry!r}"
        assert entry.strip(), "empty glyph entry"
