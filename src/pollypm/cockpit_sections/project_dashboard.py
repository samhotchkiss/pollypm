"""Per-project dashboard orchestrator (#403).

``_render_project_dashboard`` threads a single hydrated task list through
the section helpers so a render performs one SQLite open per project.
``_dashboard_project_tasks`` exposes the same gather to the global
dashboard with an mtime-keyed cache so unchanged projects skip re-reads
on every cockpit tick.
"""

from __future__ import annotations

from pathlib import Path

from pollypm.cockpit_sections.action_bar import render_project_action_bar
from pollypm.cockpit_sections.activity import _section_activity
from pollypm.cockpit_sections.base import (
    _DASHBOARD_BULLET,
    _DASHBOARD_DIVIDER_WIDTH,
    _aggregate_project_tokens,
    _iso_to_dt,
)
from pollypm.cockpit_sections.downtime import _section_downtime
from pollypm.cockpit_sections.header import _section_header, _worker_presence
from pollypm.cockpit_sections.health import format_project_health_scorecard
from pollypm.cockpit_sections.in_flight import _section_in_flight
from pollypm.cockpit_sections.insights import _section_insights
from pollypm.cockpit_sections.quick_actions import _section_quick_actions
from pollypm.cockpit_sections.recent import _section_recent
from pollypm.cockpit_sections.summary import _section_summary
from pollypm.cockpit_sections.velocity import _section_velocity
from pollypm.cockpit_sections.you_need_to import _section_you_need_to


# Cache: project_key -> (db_mtime, partitioned, counts).
_DASHBOARD_PROJECT_CACHE: dict[str, tuple[float, dict[str, list], dict[str, int]]] = {}


def _dashboard_project_tasks(
    project_key: str, project_path: Path,
) -> tuple[dict[str, list], dict[str, int]]:
    """Return ({status -> [tasks]}, state_counts) for a project, cached by db_mtime.

    At scale (100+ projects) this is the top hot path inside _build_dashboard:
    previously every render opened SQLiteWorkService per project and hydrated
    the full task list. Projects that haven't changed since last render reuse
    the cached partition, so the dashboard's cost scales with changed projects,
    not total projects.
    """
    db_path = project_path / ".pollypm" / "state.db"
    if not db_path.exists():
        return {}, {}
    try:
        db_mtime = db_path.stat().st_mtime
    except OSError:
        return {}, {}
    cached = _DASHBOARD_PROJECT_CACHE.get(project_key)
    if cached is not None and cached[0] == db_mtime:
        return cached[1], cached[2]

    from pollypm.work.sqlite_service import SQLiteWorkService
    partitioned: dict[str, list] = {
        "in_progress": [], "review": [], "queued": [], "blocked": [], "done": [],
    }
    counts: dict[str, int] = {}
    try:
        with SQLiteWorkService(db_path=db_path, project_path=project_path) as svc:
            tasks = svc.list_tasks(project=project_key)
            counts = svc.state_counts(project=project_key)
            for t in tasks:
                sv = t.work_status.value
                if sv in partitioned:
                    partitioned[sv].append(t)
    except Exception:  # noqa: BLE001
        return {}, {}

    _DASHBOARD_PROJECT_CACHE[project_key] = (db_mtime, partitioned, counts)
    return partitioned, counts


def _render_project_dashboard(
    project: object,
    project_key: str,
    config_path,
    supervisor,
) -> str | None:
    """Info-dense per-project dashboard (spec: #245).

    Sections (top to bottom): header, summary bar, velocity/cycle/tokens,
    "you need to" (approvals + alerts + pending insights), in-flight
    tasks, most-recent completion, 24h activity timeline, advisor
    insights (7d), downtime backlog, quick-action hotkeys.

    Each section is rendered by a dedicated ``_section_*`` helper that
    degrades gracefully on missing data so a fresh project with empty
    state still produces a readable surface.
    """
    from pollypm.work.sqlite_service import SQLiteWorkService

    db_path = project.path / ".pollypm" / "state.db"
    if not db_path.exists():
        return None

    # Single SQLite open per render \u2014 every downstream section reuses
    # this hydrated task list and the counts map.
    inbox_count = 0
    with SQLiteWorkService(db_path=db_path, project_path=project.path) as svc:
        try:
            from pollypm.work.inbox_view import inbox_tasks
        except Exception:  # noqa: BLE001
            inbox_tasks = None
        counts = svc.state_counts(project=project_key)
        tasks = svc.list_tasks(project=project_key)
        if inbox_tasks is not None:
            try:
                inbox_count = len(inbox_tasks(svc, project=project_key))
            except Exception:  # noqa: BLE001
                inbox_count = 0

    tokens = _aggregate_project_tokens(db_path, project_key)

    name = getattr(project, "name", None) or project_key

    # Partition tasks for downstream sections.
    in_progress = [
        t for t in tasks if t.work_status.value == "in_progress"
    ]
    review = [t for t in tasks if t.work_status.value == "review"]
    completed = [t for t in tasks if t.work_status.value == "done"]
    completed.sort(
        key=lambda t: _iso_to_dt(t.updated_at) or 0,
        reverse=True,
    )

    # Project-scoped alerts, filtered the same way the legacy renderer did.
    project_alerts: list = []
    try:
        project_alerts = [
            a for a in supervisor.store.open_alerts()
            if any(
                l.session.project == project_key
                and l.session.name == a.session_name
                for l in supervisor.plan_launches()
            )
            and a.alert_type
            not in ("suspected_loop", "stabilize_failed", "needs_followup")
        ]
    except Exception:  # noqa: BLE001
        project_alerts = []

    try:
        system_events = supervisor.store.recent_events(limit=200)
    except Exception:  # noqa: BLE001
        system_events = []
    system_events = [
        e for e in system_events
        if any(
            l.session.project == project_key
            and l.session.name == getattr(e, "session_name", None)
            for l in (
                supervisor.plan_launches()
                if hasattr(supervisor, "plan_launches")
                else []
            )
        )
    ] if system_events else []

    presence = _worker_presence(supervisor, project_key)

    out: list[str] = [
        _section_header(name, presence),
        _DASHBOARD_BULLET + "\u2500" * (_DASHBOARD_DIVIDER_WIDTH - 2),
        _DASHBOARD_BULLET
        + render_project_action_bar(
            review_count=len(review),
            alert_count=len(project_alerts),
            inbox_count=inbox_count,
        ),
        _section_summary(counts),
        format_project_health_scorecard(name, counts, tasks),
        "",
    ]
    velocity_lines = _section_velocity(tasks, tokens)
    if velocity_lines:
        out.extend(velocity_lines)
        out.append("")

    out.extend(_section_you_need_to(review, project_alerts, 0))
    out.extend(_section_in_flight(in_progress))
    out.extend(_section_recent(completed))
    out.extend(_section_activity(tasks, system_events))
    out.extend(_section_insights(project.path, project_key))
    out.extend(_section_downtime(project.path))
    out.extend(_section_quick_actions())

    return "\n".join(out)
