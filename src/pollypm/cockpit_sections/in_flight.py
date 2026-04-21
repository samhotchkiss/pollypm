"""In-flight tasks section (#403)."""

from __future__ import annotations

from pollypm.cockpit_task_priority import priority_glyph, priority_rank
from pollypm.cockpit_sections.base import (
    _DASHBOARD_BULLET,
    _STATUS_ICONS,
    _age_from_dt,
    _dashboard_divider,
    _iso_to_dt,
)


def _in_flight_sort_key(task) -> tuple[int, float, int]:
    updated = _iso_to_dt(getattr(task, "updated_at", None))
    return (
        priority_rank(task),
        -(updated.timestamp() if updated is not None else 0.0),
        int(getattr(task, "task_number", 0) or 0),
    )


def _task_dependency_numbers(task) -> list[int]:
    deps = getattr(task, "blocked_by", None) or []
    numbers: list[int] = []
    for dep in deps:
        if not isinstance(dep, tuple) or len(dep) != 2:
            continue
        try:
            numbers.append(int(dep[1]))
        except (TypeError, ValueError):
            continue
    return numbers


def _render_in_flight_row(task, *, prefix: str = "") -> str:
    icon = _STATUS_ICONS.get(task.work_status.value, "\u27f3")
    assignee = f" [{task.assignee}]" if getattr(task, "assignee", None) else ""
    node = (
        f" @ {task.current_node_id}"
        if getattr(task, "current_node_id", None)
        else ""
    )
    age = _age_from_dt(_iso_to_dt(getattr(task, "updated_at", None)))
    age_part = f" \u00b7 {age}" if age else ""
    return (
        f"{_DASHBOARD_BULLET}{prefix}{icon} {priority_glyph(task)} "
        f"#{task.task_number} {task.title}{assignee}{node}{age_part}"
    )


def _section_in_flight(in_progress: list, blocked: list | None = None) -> list[str]:
    """Tasks currently being worked on, with blocked dependents inline."""
    lines = [_dashboard_divider("In flight"), ""]
    blocked = blocked or []
    if not in_progress and not blocked:
        lines.extend([f"{_DASHBOARD_BULLET}(none)", ""])
        return lines
    ordered = sorted(in_progress, key=_in_flight_sort_key)
    rendered_blocked: set[int] = set()
    for task in ordered:
        lines.append(_render_in_flight_row(task))
        blocker_number = getattr(task, "task_number", None)
        children = sorted(
            [
                blocked_task for blocked_task in blocked
                if blocker_number is not None
                and blocker_number in _task_dependency_numbers(blocked_task)
            ],
            key=_in_flight_sort_key,
        )
        for child in children:
            rendered_blocked.add(id(child))
            wait_numbers = ", ".join(
                f"#{number}" for number in _task_dependency_numbers(child)
            )
            wait_suffix = (
                f" \u00b7 waiting on {wait_numbers}" if wait_numbers else ""
            )
            lines.append(
                _render_in_flight_row(child, prefix="  \u2514\u2500 ")
                + wait_suffix
            )
    for task in sorted(blocked, key=_in_flight_sort_key):
        if id(task) in rendered_blocked:
            continue
        wait_numbers = ", ".join(
            f"#{number}" for number in _task_dependency_numbers(task)
        )
        wait_suffix = f" \u00b7 waiting on {wait_numbers}" if wait_numbers else ""
        lines.append(_render_in_flight_row(task) + wait_suffix)
    lines.append("")
    return lines
