"""Attention-first "You need to" section (#403)."""

from __future__ import annotations

from pollypm.cockpit_sections.base import _DASHBOARD_BULLET, _dashboard_divider


def _section_you_need_to(
    review_tasks: list,
    alerts: list,
    insights_pending: int,
) -> list[str]:
    """Attention-first block \u2014 approvals, alerts, advisor insights."""
    lines = [_dashboard_divider("You need to"), ""]
    if not review_tasks and not alerts and not insights_pending:
        lines.append(f"{_DASHBOARD_BULLET}Nothing pending.")
        lines.append("")
        return lines
    for t in review_tasks[:5]:
        lines.append(
            f"{_DASHBOARD_BULLET}\u25c9 approve #{t.task_number} {t.title}"
        )
    for a in alerts[:3]:
        sess = getattr(a, "session_name", "")
        kind = getattr(a, "alert_type", "")
        msg = (getattr(a, "message", "") or "")[:60]
        lines.append(f"{_DASHBOARD_BULLET}\u25b2 {sess}: {kind} \u2014 {msg}")
    if insights_pending:
        lines.append(
            f"{_DASHBOARD_BULLET}\u2726 {insights_pending} advisor "
            f"insight{'s' if insights_pending != 1 else ''} to review"
        )
    lines.append("")
    return lines
