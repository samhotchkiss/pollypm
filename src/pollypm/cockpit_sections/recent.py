"""Most-recently-completed task row (#403)."""

from __future__ import annotations

from pollypm.cockpit_sections.base import (
    _DASHBOARD_BULLET,
    _STATUS_ICONS,
    _dashboard_divider,
    _find_commit_sha,
    _format_clock,
    _iso_to_dt,
    _task_cycle_minutes,
)


def _section_recent(completed: list) -> list[str]:
    """Most recently finished task with commit SHA, cycle time, approver."""
    lines = [_dashboard_divider("Recent"), ""]
    if not completed:
        lines.extend([f"{_DASHBOARD_BULLET}(none)", ""])
        return lines
    t = completed[0]
    dt = _iso_to_dt(getattr(t, "updated_at", None))
    clock = _format_clock(dt)
    icon = _STATUS_ICONS.get(t.work_status.value, "\u2713")

    # Approver = the actor on the last done/approve transition.
    approver = ""
    for tr in reversed(getattr(t, "transitions", None) or []):
        if tr.to_state in ("done",) and tr.actor:
            approver = f"approved by {tr.actor}"
            break
    if not approver:
        approver = ""

    title = t.title[:40]
    right = f"  {approver}" if approver else ""
    lines.append(
        f"{_DASHBOARD_BULLET}{clock}  {icon} #{t.task_number} {title}{right}"
    )

    # Sub-line: commit SHA + cycle time.
    sub_parts: list[str] = []
    sha = _find_commit_sha(t)
    if sha:
        sub_parts.append(f"commit {sha}")
    cycle = _task_cycle_minutes(t)
    if cycle is not None:
        sub_parts.append(f"{cycle}m cycle")
    if sub_parts:
        lines.append(f"{_DASHBOARD_BULLET}       \u2514 " + " \u00b7 ".join(sub_parts))
    lines.append("")
    return lines
