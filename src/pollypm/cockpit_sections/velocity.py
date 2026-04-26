"""Velocity / cycle-time / cost / token aggregation rows (#403, #501)."""

from __future__ import annotations

from pollypm.cockpit_sections.base import (
    _DASHBOARD_BULLET,
    _dashboard_divider,
    _format_tokens,
    _iso_to_dt,
    _spark_bar,
    _task_cycle_minutes,
)


_BLENDED_USD_PER_1K_TOKENS = 0.01


def _done_tasks(tasks: list) -> list:
    """Newest-first list of completed tasks that shipped work."""
    from datetime import UTC, datetime

    completed = [
        t for t in tasks if getattr(t, "work_status", None) is not None
        and t.work_status.value == "done"
    ]
    completed.sort(
        key=lambda t: _iso_to_dt(t.updated_at)
        or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    return completed


def _estimate_task_cost_usd(task) -> float | None:
    """Blended token-cost estimate until model pricing lands in the ledger."""
    total_tokens = (
        int(getattr(task, "total_input_tokens", 0) or 0)
        + int(getattr(task, "total_output_tokens", 0) or 0)
    )
    if total_tokens <= 0:
        return None
    return (total_tokens / 1000.0) * _BLENDED_USD_PER_1K_TOKENS


def _format_usd(amount: float) -> str:
    return f"${amount:.2f}"


def _cost_delta_label(completed: list, now) -> str:
    """Short this-week vs last-week summary for completed-task cost."""
    this_week: list[float] = []
    last_week: list[float] = []
    for task in completed:
        dt = _iso_to_dt(getattr(task, "updated_at", None))
        cost = _estimate_task_cost_usd(task)
        if dt is None or cost is None:
            continue
        age_days = (now - dt).days
        if 0 <= age_days < 7:
            this_week.append(cost)
        elif 7 <= age_days < 14:
            last_week.append(cost)
    if not this_week or not last_week:
        return ""
    this_avg = sum(this_week) / len(this_week)
    last_avg = sum(last_week) / len(last_week)
    delta = this_avg - last_avg
    if abs(delta) < 0.005:
        return "this wk → flat vs last"
    arrow = "↗" if delta > 0 else "↘"
    sign = "+" if delta > 0 else "-"
    return f"this wk {arrow} {sign}{_format_usd(abs(delta))} vs last"


def _section_velocity(tasks: list, tokens: tuple[int, int] | None) -> list[str]:
    """Velocity + cycle-time + cost sparklines and token aggregation.

    Always returns a divider + body. When there's no data yet, emit
    a "(no shipped tasks yet)" placeholder line so the user can tell
    the section is *empty* rather than *broken / loading* — matches
    the empty-state contract used by Recent / Insights / Downtime.
    Audit UX #9.
    """
    from datetime import UTC, datetime

    lines: list[str] = [_dashboard_divider("Velocity"), ""]
    body: list[str] = []
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
        body.append(
            f"{_DASHBOARD_BULLET}Velocity    {_spark_bar(weekly):<8}    "
            f"{per_week} tasks/wk, {trend}"
        )

    # Cycle time sparkline: median minutes for each of the last 7 completed tasks.
    cycles: list[int] = []
    completed = _done_tasks(tasks)
    for t in completed[:7]:
        m = _task_cycle_minutes(t)
        if m is not None:
            cycles.append(m)
    if cycles:
        avg_min = sum(cycles) // len(cycles)
        cycles_asc = list(reversed(cycles))  # oldest-left, newest-right
        body.append(
            f"{_DASHBOARD_BULLET}Cycle time  {_spark_bar(cycles_asc):<8}    "
            f"{avg_min}m avg"
        )

    # Cost sparkline: blended $ estimate from per-task token totals.
    recent_costs_desc = [
        cost for cost in (_estimate_task_cost_usd(task) for task in completed[:7])
        if cost is not None
    ]
    if recent_costs_desc:
        avg_cost = sum(recent_costs_desc) / len(recent_costs_desc)
        cost_spark_values = [
            max(1, int(round(cost * 100)))
            for cost in reversed(recent_costs_desc)
        ]
        delta_label = _cost_delta_label(completed, now)
        suffix = f" · {delta_label}" if delta_label else ""
        body.append(
            f"{_DASHBOARD_BULLET}Cost/task  {_spark_bar(cost_spark_values):<8}    "
            f"avg {_format_usd(avg_cost)}/task{suffix}"
        )

    # Token aggregation \u2014 drops the line entirely when unavailable.
    if tokens is not None:
        tin, tout = tokens
        if tin or tout:
            body.append(
                f"{_DASHBOARD_BULLET}Tokens      "
                f"{_format_tokens(tin)} in \u00b7 {_format_tokens(tout)} out"
            )
    if not body:
        # Empty-state placeholder so the section is visibly skipped
        # rather than silently absent (matches Recent / Insights /
        # Downtime contract). Audit UX #9.
        body.append(f"{_DASHBOARD_BULLET}(no shipped tasks yet)")
    lines.extend(body)
    lines.append("")
    return lines
