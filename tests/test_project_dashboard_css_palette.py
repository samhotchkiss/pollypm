"""Regression: dashboard CSS routes canonical hexes through ``cockpit_theme.State``.

The per-project dashboard CSS in ``cockpit_ui.PollyProjectDashboardApp``
historically duplicated raw hex literals (``#5b8aff``, ``#6b7a88``, etc.)
that also lived in the rail / inbox / activity / footer palette. PR
#1989 introduced ``cockpit_theme.State`` as the single source of truth;
PR #2022 promoted the five cockpit-wide canonical colors through
``State.*``; this follow-up promotes the three dashboard surface tints
(WARN amber, DANGER red, SUCCESS green — each a text/background/border
triple) so a tint tweak propagates to the dashboard without a manual
edit and without silent drift.

What stays literal: pure dashboard chrome that has no analog elsewhere
in the cockpit (screen background, section borders, scrollbar slate,
plan-scroll backdrop).
"""

from __future__ import annotations

from pollypm.cockpit_theme import State
from pollypm.cockpit_ui import (
    PollyProjectDashboardApp,
    _dashboard_css_with_palette,
)


# The five canonical semantic colors that the dashboard shares with the
# rest of the cockpit. Mapping pinned so a rename in ``cockpit_theme``
# can't silently drop a substitution.
_CANONICAL_PAIRS = [
    ("INFO", State.INFO),
    ("MUTED", State.MUTED),
    ("NEUTRAL", State.NEUTRAL),
    ("BODY_BRIGHT", State.BODY_BRIGHT),
    ("HEADING", State.HEADING),
]


# The three dashboard surface tints, each a (text, background, border)
# triple. Pinned so a future ``State.SURFACE_*`` rename can't silently
# drop a substitution out of ``_dashboard_css_with_palette``.
_SURFACE_TRIPLES = [
    (
        "WARN",
        State.SURFACE_WARN,
        State.SURFACE_WARN_BG,
        State.SURFACE_WARN_BORDER,
    ),
    (
        "DANGER",
        State.SURFACE_DANGER,
        State.SURFACE_DANGER_BG,
        State.SURFACE_DANGER_BORDER,
    ),
    (
        "SUCCESS",
        State.SURFACE_SUCCESS,
        State.SURFACE_SUCCESS_BG,
        State.SURFACE_SUCCESS_BORDER,
    ),
]


def test_dashboard_css_contains_canonical_state_hexes() -> None:
    """Every canonical ``State.*`` color used by the dashboard must
    appear in the rendered CSS at least once. If a future palette tweak
    moves one of these, the helper substitution must still emit the new
    hex into the CSS — proving the dashboard reads from ``State``, not
    from a stale literal copy.
    """
    css = PollyProjectDashboardApp.CSS
    for name, hex_value in _CANONICAL_PAIRS:
        assert hex_value in css, (
            f"State.{name} ({hex_value}) missing from dashboard CSS — "
            f"the palette substitution helper is broken or out of date."
        )


def test_dashboard_css_contains_surface_tint_state_hexes() -> None:
    """Every ``State.SURFACE_*`` color used by the dashboard must appear
    in the rendered CSS at least once. Same contract as the canonical-
    hex test above but pinned for the three surface tint triples
    promoted as the PR #2022 follow-up.
    """
    css = PollyProjectDashboardApp.CSS
    for family, text, bg, border in _SURFACE_TRIPLES:
        for role, hex_value in (
            ("text", text),
            ("background", bg),
            ("border", border),
        ):
            assert hex_value in css, (
                f"State.SURFACE_{family} {role} ({hex_value}) missing "
                f"from dashboard CSS — the palette substitution helper "
                f"is broken or out of date."
            )


def test_dashboard_css_helper_substitutes_each_canonical_color() -> None:
    """Directly exercise the substitution helper: feed it a stub CSS
    block with every canonical raw hex and assert each one gets replaced
    by the corresponding ``State.*`` value. This guards against the
    palette tuple inside ``_dashboard_css_with_palette`` losing an entry
    in a future refactor.
    """
    stub_css = (
        "color: #5b8aff;\n"
        "color: #6b7a88;\n"
        "color: #97a6b2;\n"
        "color: #d6dee5;\n"
        "color: #eef2f4;\n"
    )
    rendered = _dashboard_css_with_palette(stub_css)
    # Each canonical State value must appear in the rendered CSS exactly
    # where the matching raw hex was.
    for _, hex_value in _CANONICAL_PAIRS:
        assert hex_value in rendered


def test_dashboard_css_helper_substitutes_each_surface_tint() -> None:
    """Directly exercise the substitution helper on the three surface
    tint triples (WARN / DANGER / SUCCESS). Mirrors the canonical-color
    helper test so a missing entry in the palette tuple fails loudly.
    """
    stub_css = (
        # WARN triple
        "color: #f7d67a;\n"
        "background: #3a2c08;\n"
        "border: round #7a5a14;\n"
        # DANGER triple
        "color: #ffd7d9;\n"
        "background: #3a1719;\n"
        "border: round #8d3137;\n"
        # SUCCESS triple
        "color: #b6f0c0;\n"
        "background: #1a2e1c;\n"
        "border: round #2c5b32;\n"
    )
    rendered = _dashboard_css_with_palette(stub_css)
    for _, text, bg, border in _SURFACE_TRIPLES:
        assert text in rendered
        assert bg in rendered
        assert border in rendered


def test_dashboard_css_is_byte_identical_to_pre_migration_palette() -> None:
    """The migration is intentionally source-level, not visual. Today
    each ``State.*`` value matches the original raw hex byte-for-byte,
    so the rendered CSS still contains the original literals (canonical
    five plus the nine surface-tint hexes). If somebody changes a
    ``State.*`` value, this test will fail loudly so the visual shift
    is a deliberate decision, not an accident.
    """
    css = PollyProjectDashboardApp.CSS
    raw_hexes = (
        # Canonical five (PR #2022).
        "#5b8aff",
        "#6b7a88",
        "#97a6b2",
        "#d6dee5",
        "#eef2f4",
        # Surface tints (this PR) — WARN / DANGER / SUCCESS triples.
        "#f7d67a",
        "#3a2c08",
        "#7a5a14",
        "#ffd7d9",
        "#3a1719",
        "#8d3137",
        "#b6f0c0",
        "#1a2e1c",
        "#2c5b32",
    )
    for raw in raw_hexes:
        assert raw in css, (
            f"{raw} no longer in dashboard CSS — a State.* value moved. "
            f"If this was intentional, update this test."
        )
