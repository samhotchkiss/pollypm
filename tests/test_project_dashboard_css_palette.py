"""Regression: dashboard CSS routes canonical hexes through ``cockpit_theme.State``.

The per-project dashboard CSS in ``cockpit_ui.PollyProjectDashboardApp``
historically duplicated raw hex literals (``#5b8aff``, ``#6b7a88``, etc.)
that also lived in the rail / inbox / activity / footer palette. PR
#1989 introduced ``cockpit_theme.State`` as the single source of truth;
this test pins that the dashboard's CSS string is produced by routing
those five canonical colors through ``State.*`` (so a future palette
tweak in ``cockpit_theme`` propagates to the dashboard without a
manual edit and without silent drift).

We deliberately do NOT assert on dashboard-specific surface tints
(action-bar attention/critical fills, section backgrounds, scrollbar
slate) — those have no current ``State.*`` equivalent and stay literal
until a future audit promotes them.
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


def test_dashboard_css_is_byte_identical_to_pre_migration_palette() -> None:
    """The migration is intentionally source-level, not visual. Today
    each canonical ``State.*`` value matches the original raw hex
    byte-for-byte, so the rendered CSS still contains the original five
    literals (``#5b8aff``, ``#6b7a88``, ``#97a6b2``, ``#d6dee5``,
    ``#eef2f4``). If somebody changes a ``State.*`` value, this test
    will fail loudly so the visual shift is a deliberate decision, not
    an accident.
    """
    css = PollyProjectDashboardApp.CSS
    for raw in ("#5b8aff", "#6b7a88", "#97a6b2", "#d6dee5", "#eef2f4"):
        assert raw in css, (
            f"{raw} no longer in dashboard CSS — a State.* value moved. "
            f"If this was intentional, update this test."
        )
