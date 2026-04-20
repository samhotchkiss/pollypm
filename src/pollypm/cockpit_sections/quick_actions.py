"""Quick-actions hotkey hints section (#403)."""

from __future__ import annotations

from pollypm.cockpit_sections.base import _DASHBOARD_BULLET, _dashboard_divider


def _section_quick_actions() -> list[str]:
    """Row of keyboard hotkey hints."""
    return [
        _dashboard_divider("Quick actions"),
        "",
        f"{_DASHBOARD_BULLET}n  new task    w  start worker    r  replan    "
        f"i  inbox    c  chat",
    ]
