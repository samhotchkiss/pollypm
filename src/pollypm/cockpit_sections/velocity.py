"""Velocity / cycle-time / token aggregation rows (#403)."""

from __future__ import annotations

from pollypm.cockpit_sections.base import (
    _DASHBOARD_BULLET,
    _format_tokens,
    _iso_to_dt,
    _spark_bar,
    _task_cycle_minutes,
)


def _section_velocity(tasks: list, tokens: tuple[int, int] | None) -> list[str]:
    """Velocity + cycle-time sparklines and token aggregation."""
    from datetime import UTC, datetime

    lines: list[str] = []
    now = datetime.now(UTC)

    # Weekly velocity over the last 7 weeks: count of tasks that hit a
    # terminal state in each week. The sparkline reads left-to-right
    # oldest \u2192 newest.
    weekly: list[int] = [0] * 7
    for t in tasks:
        if getattr(t, "work_status", None) is None:
            continue
        if t.work_status.value not in ("done", "cancelled"):
            continue
        dt = _iso_to_dt(t.updated_at)
        if dt is None:
            continue
        age_days = (now - dt).days
        if age_days < 0 or age_days >= 49:
            continue
        week_idx = 6 - (age_days // 7)
        if 0 <= week_idx < 7:
            weekly[week_idx] += 1
    if any(weekly):
        per_week = weekly[-1]
        trend = (
            "trending up" if weekly[-1] > weekly[0] + 1 else
            "trending down" if weekly[-1] + 1 < weekly[0] else
            "steady"
        )
        lines.append(
            f"{_DASHBOARD_BULLET}Velocity    {_spark_bar(weekly):<8}    "
            f"{per_week} tasks/wk, {trend}"
        )

    # Cycle time sparkline: median minutes for each of the last 7 completed tasks.
    cycles: list[int] = []
    completed = [
        t for t in tasks if getattr(t, "work_status", None) is not None
        and t.work_status.value == "done"
    ]
    completed.sort(
        key=lambda t: _iso_to_dt(t.updated_at)
        or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    for t in completed[:7]:
        m = _task_cycle_minutes(t)
        if m is not None:
            cycles.append(m)
    if cycles:
        avg_min = sum(cycles) // len(cycles)
        cycles_asc = list(reversed(cycles))  # oldest-left, newest-right
        lines.append(
            f"{_DASHBOARD_BULLET}Cycle time  {_spark_bar(cycles_asc):<8}    "
            f"{avg_min}m avg"
        )

    # Token aggregation \u2014 drops the line entirely when unavailable.
    if tokens is not None:
        tin, tout = tokens
        if tin or tout:
            lines.append(
                f"{_DASHBOARD_BULLET}Tokens      "
                f"{_format_tokens(tin)} in \u00b7 {_format_tokens(tout)} out"
            )
    return lines
