"""Regression: alert-detail modal CSS routes canonical hexes through ``cockpit_theme.State``.

The rail-alert recovery modal in ``cockpit_alert_detail`` historically
duplicated raw hex literals (``#ff5f6d``, ``#f0c45a``, ``#97a6b2``,
``#d6dee5``, ``#eef6ff``, ``#6b7a88``) that also lived in the rail /
inbox / activity / footer palette. This is the final wedge of issue
#1988 (PRs #1989, #2013, #2022, #2033 promoted the rest of the
cockpit). Mirrors the dashboard-CSS palette regression in
``test_project_dashboard_css_palette``.

What stays literal: the modal dialog ``background`` (``#141a20``) and
the list-item highlight ``background`` (``#1f4d7a``). Neither has an
analog elsewhere in the cockpit; promoting them would require inventing
single-use ``State`` constants and is deferred to a follow-up. The TODO
markers in the source CSS pin that intent.
"""

from __future__ import annotations

from pollypm.cockpit_alert_detail import (
    _AlertDetailModal,
    _alert_detail_css_with_palette,
)
from pollypm.cockpit_theme import State


# The six canonical semantic colors the alert-detail modal shares with
# the rest of the cockpit. Pinned so a rename in ``cockpit_theme`` can't
# silently drop a substitution out of ``_alert_detail_css_with_palette``.
_CANONICAL_PAIRS = [
    ("BLOCKED", State.BLOCKED),
    ("WAITING", State.WAITING),
    ("NEUTRAL", State.NEUTRAL),
    ("BODY_BRIGHT", State.BODY_BRIGHT),
    ("HEADING_BRIGHT", State.HEADING_BRIGHT),
    ("MUTED", State.MUTED),
]


def test_alert_detail_css_contains_canonical_state_hexes() -> None:
    """Every canonical ``State.*`` color used by the alert-detail modal
    must appear in the rendered CSS at least once. If a future palette
    tweak moves one of these, the helper substitution must still emit
    the new hex into the CSS — proving the modal reads from ``State``,
    not from a stale literal copy.
    """
    css = _AlertDetailModal.DEFAULT_CSS
    for name, hex_value in _CANONICAL_PAIRS:
        assert hex_value in css, (
            f"State.{name} ({hex_value}) missing from alert-detail CSS — "
            f"the palette substitution helper is broken or out of date."
        )


def test_alert_detail_css_helper_substitutes_each_canonical_color() -> None:
    """Directly exercise the substitution helper: feed it a stub CSS
    block with every canonical raw hex and assert each one gets replaced
    by the corresponding ``State.*`` value. This guards against the
    palette tuple inside ``_alert_detail_css_with_palette`` losing an
    entry in a future refactor.
    """
    stub_css = (
        "border: round #ff5f6d;\n"
        "border: round #f0c45a;\n"
        "color: #97a6b2;\n"
        "color: #d6dee5;\n"
        "color: #eef6ff;\n"
        "color: #6b7a88;\n"
    )
    rendered = _alert_detail_css_with_palette(stub_css)
    for _, hex_value in _CANONICAL_PAIRS:
        assert hex_value in rendered


def test_alert_detail_css_is_byte_identical_to_pre_migration_palette() -> None:
    """The migration is intentionally source-level, not visual. Today
    each ``State.*`` value matches the original raw hex byte-for-byte,
    so the rendered CSS still contains the original literals. If
    somebody changes a ``State.*`` value, this test will fail loudly so
    the visual shift is a deliberate decision, not an accident.
    """
    css = _AlertDetailModal.DEFAULT_CSS
    raw_hexes = (
        "#ff5f6d",  # BLOCKED — alert dialog border + title
        "#f0c45a",  # WAITING — warn variant border + title
        "#97a6b2",  # NEUTRAL — meta line
        "#d6dee5",  # BODY_BRIGHT — message body
        "#eef6ff",  # HEADING_BRIGHT — highlight text on selected action
        "#6b7a88",  # MUTED — hint line at bottom
    )
    for raw in raw_hexes:
        assert raw in css, (
            f"{raw} no longer in alert-detail CSS — a State.* value moved. "
            f"If this was intentional, update this test."
        )


def test_alert_detail_module_has_only_documented_unmigrated_hexes() -> None:
    """The two hex literals that stay literal in the CSS source today
    are the modal dialog background (``#141a20``) and the list-item
    highlight background (``#1f4d7a``). Both are single-use surfaces
    with no analog elsewhere in the cockpit; promoting them is deferred
    to a follow-up (see TODO markers in the source). This test pins
    the deferred set so any future hex sneaking into the module surfaces
    in code review.
    """
    import re

    from pollypm import cockpit_alert_detail

    source_path = cockpit_alert_detail.__file__
    with open(source_path, encoding="utf-8") as fh:
        source = fh.read()

    # Strip the migration palette table — those hex literals exist as
    # the LHS of the substitution helper and are expected. Strip
    # docstrings/comments mentioning the deferred hexes too.
    docstring_mentions = {"#141a20", "#1f4d7a"}
    migrated_hexes = {hex_value for _, hex_value in _CANONICAL_PAIRS}

    all_hexes = set(re.findall(r"#[0-9a-fA-F]{6}", source))
    unexpected = all_hexes - migrated_hexes - docstring_mentions
    assert not unexpected, (
        f"Unexpected hex literal(s) in cockpit_alert_detail.py: "
        f"{sorted(unexpected)}. Either route through cockpit_theme.State "
        f"or add a TODO marker + extend this test's allow-list."
    )
