"""Project-health scorecards for cockpit dashboard surfaces.

Contract:
- Inputs: per-project task counts, hydrated task rows, and an optional
  ``now`` timestamp for deterministic tests.
- Outputs: one-line scorecards plus sortable health ranks for global and
  per-project cockpit dashboards.
- Side effects: none.
- Invariants: missing timestamps / transitions degrade to neutral values
  instead of raising so dashboards remain readable on partial state.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pollypm.cockpit_sections.base import _iso_to_dt, _task_cycle_minutes

_ACTIVE_STATUSES = {"draft", "queued", "in_progress", "review", "blocked", "on_hold"}
_STUCK_AFTER = timedelta(hours=6)


def _project_cycle_minutes(tasks: list) -> int | None:
    completed = [
        task for task in tasks
        if getattr(getattr(task, "work_status", None), "value", None) == "done"
    ]
    completed.sort(
        key=lambda task: _iso_to_dt(getattr(task, "updated_at", None))
        or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    cycles = [
        minutes for task in completed[:7]
        if (minutes := _task_cycle_minutes(task)) is not None
    ]
    if not cycles:
        return None
    return sum(cycles) // len(cycles)


def stuck_task_count(tasks: list, *, now: datetime | None = None) -> int:
    """Count non-terminal tasks whose activity is stale beyond the threshold."""
    now = now or datetime.now(UTC)
    stuck = 0
    for task in tasks:
        status = getattr(getattr(task, "work_status", None), "value", None) or str(
            getattr(task, "work_status", "") or ""
        )
        if status not in _ACTIVE_STATUSES:
            continue
        updated = _iso_to_dt(getattr(task, "updated_at", None))
        if updated is None:
            continue
        if now - updated > _STUCK_AFTER:
            stuck += 1
    return stuck


def project_health_glyph(tasks: list, *, now: datetime | None = None) -> str:
    """Return the health glyph based on the number of stuck tasks."""
    stuck = stuck_task_count(tasks, now=now)
    if stuck > 2:
        return "🔴"
    if stuck >= 1:
        return "🟡"
    return "🟢"


def project_health_rank(tasks: list, *, now: datetime | None = None) -> int:
    """Sort worst health first so the cockpit surfaces stuck projects early."""
    glyph = project_health_glyph(tasks, now=now)
    if glyph == "🔴":
        return 0
    if glyph == "🟡":
        return 1
    return 2


def format_project_health_scorecard(
    project_name: str,
    counts: dict[str, int],
    tasks: list,
    *,
    now: datetime | None = None,
) -> str:
    """Format the cockpit one-line project health summary."""
    cycle = _project_cycle_minutes(tasks)
    cycle_part = f"{cycle}m cycle" if cycle is not None else "— cycle"
    return (
        f"{project_name} · "
        f"{int(counts.get('in_progress', 0))} in progress · "
        f"{int(counts.get('review', 0))} review · "
        f"{int(counts.get('blocked', 0))} blocked · "
        f"{cycle_part} · "
        f"{project_health_glyph(tasks, now=now)}"
    )
