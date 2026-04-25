"""Sticky one-line attention summary for project dashboards."""

from __future__ import annotations


def render_project_action_bar(
    *,
    review_count: int,
    alert_count: int,
    inbox_count: int,
    blocker_count: int = 0,
    on_hold_count: int = 0,
) -> str:
    """Return a compact summary of the project's actionable work."""
    bits: list[str] = []
    if blocker_count:
        bits.append(
            f"{blocker_count} waiting on dependenc"
            f"{'ies' if blocker_count != 1 else 'y'}"
        )
    if on_hold_count:
        bits.append(f"{on_hold_count} on hold")
    if review_count:
        bits.append(f"{review_count} approval{'s' if review_count != 1 else ''}")
    if alert_count:
        bits.append(f"{alert_count} alert{'s' if alert_count != 1 else ''}")
    if inbox_count:
        bits.append(f"{inbox_count} need action")
    if not bits:
        return "▸ Clear · no approvals, alerts, or user actions"
    return "▸ " + " · ".join(bits)
