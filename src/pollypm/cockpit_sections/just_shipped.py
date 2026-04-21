"""Celebratory dashboard section for recently approved work."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pollypm.cockpit_sections.base import (
    _DASHBOARD_BULLET,
    _dashboard_divider,
    _iso_to_dt,
    _task_cycle_minutes,
)


def _relative_age(value, *, now: datetime) -> str:
    dt = _iso_to_dt(value)
    if dt is None:
        return ""
    delta = now - dt
    if delta < timedelta(minutes=1):
        return "just now"
    if delta < timedelta(hours=1):
        return f"{int(delta.total_seconds() // 60)}m ago"
    if delta < timedelta(days=1):
        return f"{int(delta.total_seconds() // 3600)}h ago"
    return f"{delta.days}d ago"


def _section_just_shipped(
    completed: list[tuple[str, object]],
    *,
    now: datetime | None = None,
) -> list[str]:
    """Render the last three approved tasks from the last 24 hours."""
    now = now or datetime.now(UTC)
    shipped: list[tuple[str, object]] = []
    cutoff = now - timedelta(hours=24)
    for project_key, task in completed:
        updated = _iso_to_dt(getattr(task, "updated_at", None))
        if updated is None or updated < cutoff:
            continue
        shipped.append((project_key, task))
        if len(shipped) == 3:
            break
    if not shipped:
        return []

    lines = [_dashboard_divider("🎉 Just shipped"), ""]
    for project_key, task in shipped:
        cycle = _task_cycle_minutes(task)
        cycle_part = f"{cycle}m cycle" if cycle is not None else "— cycle"
        age = _relative_age(getattr(task, "updated_at", None), now=now)
        lines.append(
            f"{_DASHBOARD_BULLET}{project_key}/{task.task_number}  "
            f"{cycle_part:<9}  {age}"
        )
    lines.append("")
    return lines
