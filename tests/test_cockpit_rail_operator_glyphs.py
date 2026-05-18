"""Rail glyph vocabulary tests (#1572).

Pins the load-bearing invariant from the issue spec: the section a
project lands in on the operator dashboard MUST match the glyph the
rail paints for it. Both surfaces read from
:func:`pollypm.dashboard.categorize_project`; the test exercises the
rail's ``_indicator`` directly against a synthetic ``CockpitItem``
seeded with the per-project state the dashboard would compute, then
compares the glyph to the dashboard's ``glyph_for_project_state``.

The check is intentionally low-level: ``_indicator`` is the rail's
glyph selector and the dashboard's ``glyph_for_project_state`` is the
canonical glyph table. If a future change drifts one without the
other, this test breaks before the user notices.
"""

from __future__ import annotations

from types import SimpleNamespace

from pollypm.cockpit_rail import CockpitItem, PollyCockpitRail
from pollypm.dashboard import (
    ProjectState,
    glyph_for_project_state,
)


def _rail() -> PollyCockpitRail:
    """Build a rail instance without running it (no I/O, no config load)."""
    return PollyCockpitRail.__new__(PollyCockpitRail)


def _item(*, project_state: str | None) -> CockpitItem:
    return CockpitItem(
        key="project:demo",
        label="demo",
        state="project-green",
        project_state=project_state,
    )


def test_rail_indicator_uses_waiting_glyph() -> None:
    rail = _rail()
    glyph, _color = rail._indicator(_item(project_state="waiting"))
    assert glyph == glyph_for_project_state(ProjectState.WAITING)


def test_rail_indicator_uses_working_glyph() -> None:
    rail = _rail()
    glyph, _color = rail._indicator(_item(project_state="working"))
    assert glyph == glyph_for_project_state(ProjectState.WORKING)


def test_rail_indicator_uses_idle_glyph() -> None:
    rail = _rail()
    glyph, _color = rail._indicator(_item(project_state="idle"))
    assert glyph == glyph_for_project_state(ProjectState.IDLE)


def test_rail_indicator_uses_paused_glyph() -> None:
    rail = _rail()
    glyph, _color = rail._indicator(_item(project_state="paused"))
    assert glyph == glyph_for_project_state(ProjectState.PAUSED)


def test_rail_indicator_categorization_overrides_legacy_state() -> None:
    """When categorization is present, the new vocabulary wins.

    The legacy ``state="project-green"`` field would have selected the
    ``•`` (small bullet) glyph; the new ``project_state="working"``
    must override it to ``●`` so the rail and the dashboard agree.
    """
    rail = _rail()
    glyph, _color = rail._indicator(
        CockpitItem(
            key="project:demo",
            label="demo",
            state="project-green",
            project_state="working",
        )
    )
    assert glyph == "●"


def test_rail_indicator_operational_red_still_wins() -> None:
    """Operational fault keeps its dedicated glyph even with categorization."""
    rail = _rail()
    glyph, _color = rail._indicator(
        CockpitItem(
            key="project:demo",
            label="demo",
            state="project-red",
            project_state="waiting",
            alert_severity="error",
        )
    )
    assert glyph == "▲"  # ▲ operational alert


def test_rail_indicator_approvals_pending_still_wins() -> None:
    """Approvals-pending ▶ marker keeps precedence over the new glyphs."""
    rail = _rail()
    glyph, _color = rail._indicator(
        CockpitItem(
            key="project:demo",
            label="demo",
            state="project-green",
            project_state="working",
            approvals_pending=2,
        )
    )
    assert glyph == "▶"  # ▶ — the approvals-pending affordance


def test_dashboard_and_rail_agree_for_every_state() -> None:
    """Same enum drives both surfaces — the per-row check that pins
    the invariant the user can see at a glance."""
    rail = _rail()
    for state in ProjectState:
        expected = glyph_for_project_state(state)
        item = _item(project_state=state.value)
        glyph, _color = rail._indicator(item)
        assert glyph == expected, (
            f"rail/dashboard glyph drift for {state.value}: "
            f"rail={glyph!r} dashboard={expected!r}"
        )
