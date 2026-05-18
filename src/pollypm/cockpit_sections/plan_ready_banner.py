"""Plan-ready banner for the per-project dashboard (#1633).

Surfaces a compact banner when a ``plan_project`` task has reached
``done`` but the user hasn't seen the canonical plan-review surface
(either because the watchdog bypassed the approval, or because the
project drilldown is just rendering the regular dashboard for some
other reason). The banner tells the user the plan is complete, names
the downstream task count that's ready to start, and points at the
[a] Approve hotkey.

This is a SECONDARY surface — :func:`find_actionable_plan_review_task`
still owns the full plan-review render. The banner only fires when the
full surface would NOT (i.e. ``find_actionable_plan_review_task``
returned ``None`` for the project) but a done plan_project task still
exists. Rule 3 in the #1633 plan: "a complete plan IS a summary."

The function returns a small list of lines so the orchestrator can
inline-extend the dashboard ``out`` list, matching the contract of
every other ``_section_*`` helper.
"""

from __future__ import annotations

from typing import Any

from pollypm.cockpit_sections.base import (
    _DASHBOARD_BULLET,
    _DASHBOARD_DIVIDER_WIDTH,
    _age_from_dt,
    _dashboard_divider,
    _iso_to_dt,
)


# Statuses that count as "downstream task ready to start" once the
# plan completes. Queued is the strongest signal — those are the
# architect's stage-8 emit output. Blocked counts too because a task
# blocked on the plan gate today becomes unblocked the moment the
# plan approval lands; we want the user to see how much work is
# parked behind the gate.
_READY_DOWNSTREAM_STATUSES: frozenset[str] = frozenset({"queued", "blocked"})


def _task_status_value(task: Any) -> str:
    """Return the status string for a task object."""
    status = getattr(task, "work_status", None)
    if status is None:
        status = getattr(task, "status", None)
    return str(getattr(status, "value", status) or "")


def find_done_plan_project_task(tasks: list) -> Any | None:
    """Return the most-recently-updated ``plan_project`` task in ``done``.

    Walks the project's task list (already hydrated by the dashboard
    orchestrator) and picks the freshest done plan_project task, so a
    re-plan produces the latest banner without manual disambiguation.

    Returns ``None`` when no done plan_project task exists — the
    banner section is then a no-op and the dashboard falls through to
    its regular sections.
    """
    candidates: list[Any] = []
    for task in tasks or []:
        flow_id = getattr(task, "flow_template_id", "") or ""
        if flow_id != "plan_project":
            continue
        if _task_status_value(task) != "done":
            continue
        candidates.append(task)
    if not candidates:
        return None
    candidates.sort(
        key=lambda t: _iso_to_dt(getattr(t, "updated_at", None)) or 0,
        reverse=True,
    )
    return candidates[0]


def _count_downstream_ready(tasks: list, *, plan_task: Any) -> int:
    """Count tasks that read as 'ready downstream work' once the plan ships.

    Filters to ``queued`` / ``blocked`` statuses (the
    :data:`_READY_DOWNSTREAM_STATUSES` set) and excludes the
    ``plan_project`` task itself + any plan-shaped sibling. The
    architect's emit stage produces queued impl tasks; the banner
    advertises that count so the user sees how much work the plan
    unlocks.
    """
    plan_id = getattr(plan_task, "task_id", None)
    count = 0
    for task in tasks or []:
        if getattr(task, "task_id", None) == plan_id:
            continue
        flow_id = getattr(task, "flow_template_id", "") or ""
        if flow_id in {"plan_project", "critique_flow"}:
            continue
        if _task_status_value(task) in _READY_DOWNSTREAM_STATUSES:
            count += 1
    return count


def render_plan_ready_banner(
    *,
    tasks: list,
    plan_task: Any,
) -> list[str]:
    """Render the plan-ready banner lines.

    The banner is two visual rows: a header divider labelled "Plan
    ready" and a single-line message that advertises the downstream
    task count plus the [a] hotkey. Falls back gracefully when the
    plan task has no resolvable age / task_id.
    """
    downstream = _count_downstream_ready(tasks, plan_task=plan_task)
    plan_id = getattr(plan_task, "task_id", None) or "?"
    updated_at = _iso_to_dt(getattr(plan_task, "updated_at", None))
    age = _age_from_dt(updated_at) if updated_at is not None else ""

    summary_parts: list[str] = [
        f"[bold green]Plan is complete[/bold green] · {plan_id}",
    ]
    if age:
        summary_parts.append(f"approved {age}")
    if downstream:
        summary_parts.append(
            f"{downstream} task{'s' if downstream != 1 else ''} ready to start"
        )
    summary_line = _DASHBOARD_BULLET + " · ".join(summary_parts)

    cta_line = _DASHBOARD_BULLET + (
        "[bold green]\\[a][/bold green] Review plan & approve   "
        "[bold cyan]\\[c][/bold cyan] Chat to refine"
    )

    return [
        _dashboard_divider("Plan ready"),
        summary_line,
        cta_line,
        "",
    ]


def maybe_render_plan_ready_banner(
    *,
    tasks: list,
    plan_review_surface_active: bool,
) -> list[str]:
    """Return banner lines when a plan is done but no plan-review surface fires.

    Wired from :func:`pollypm.cockpit_sections.project_dashboard._render_project_dashboard`.
    The orchestrator already checks
    :func:`pollypm.cockpit_sections.plan_review.find_actionable_plan_review_task`
    and short-circuits the regular dashboard when that returns a task;
    we therefore only consider the banner when ``plan_review_surface_active``
    is ``False``. That keeps the banner from competing with the full
    surface for primacy.

    Returns ``[]`` when no done ``plan_project`` task exists — the
    section then renders nothing.
    """
    if plan_review_surface_active:
        return []
    plan_task = find_done_plan_project_task(tasks)
    if plan_task is None:
        return []
    return render_plan_ready_banner(tasks=tasks, plan_task=plan_task)


__all__ = [
    "find_done_plan_project_task",
    "render_plan_ready_banner",
    "maybe_render_plan_ready_banner",
]
