"""Per-project / global cockpit dashboard sections (#403).

Each section is a stateless helper that turns a slice of cockpit state
into a list of rendered text lines. Orchestration (which sections to
render, in what order, with what divider widths) lives in
``project_dashboard.py`` for the per-project view and ``dashboard.py``
for the global Polly dashboard.

Importers should reach into the specific submodule when adding new
sections; the names re-exported here exist so the legacy ``pollypm.cockpit``
shim and existing tests can keep importing the helpers from a single
namespace without churn.
"""

from __future__ import annotations

from pollypm.cockpit_sections.activity import _section_activity
from pollypm.cockpit_sections.base import (
    _DASHBOARD_BULLET,
    _DASHBOARD_DIVIDER_WIDTH,
    _STATUS_ICONS,
    SectionRenderer,
    _age_from_dt,
    _aggregate_project_tokens,
    _dashboard_divider,
    _find_commit_sha,
    _format_clock,
    _format_tokens,
    _iso_to_dt,
    _spark_bar,
    _task_cycle_minutes,
)
from pollypm.cockpit_sections.dashboard import _build_dashboard
from pollypm.cockpit_sections.downtime import _section_downtime
from pollypm.cockpit_sections.header import _section_header, _worker_presence
from pollypm.cockpit_sections.health import (
    format_project_health_scorecard,
    project_health_glyph,
    project_health_rank,
    stuck_task_count,
)
from pollypm.cockpit_sections.in_flight import _section_in_flight
from pollypm.cockpit_sections.insights import _section_insights
from pollypm.cockpit_sections.just_shipped import _section_just_shipped
from pollypm.cockpit_sections.project_dashboard import (
    _DASHBOARD_PROJECT_CACHE,
    _dashboard_project_tasks,
    _render_project_dashboard,
)
from pollypm.cockpit_sections.quick_actions import _section_quick_actions
from pollypm.cockpit_sections.recent import _section_recent
from pollypm.cockpit_sections.summary import _section_summary
from pollypm.cockpit_sections.velocity import _section_velocity
from pollypm.cockpit_sections.you_need_to import _section_you_need_to


__all__ = [
    "SectionRenderer",
    "_DASHBOARD_BULLET",
    "_DASHBOARD_DIVIDER_WIDTH",
    "_DASHBOARD_PROJECT_CACHE",
    "_STATUS_ICONS",
    "_age_from_dt",
    "_aggregate_project_tokens",
    "_build_dashboard",
    "_dashboard_divider",
    "_dashboard_project_tasks",
    "_find_commit_sha",
    "_format_clock",
    "_format_tokens",
    "_iso_to_dt",
    "_render_project_dashboard",
    "format_project_health_scorecard",
    "project_health_glyph",
    "project_health_rank",
    "_section_activity",
    "_section_downtime",
    "_section_header",
    "_section_in_flight",
    "_section_insights",
    "_section_just_shipped",
    "_section_quick_actions",
    "_section_recent",
    "_section_summary",
    "_section_velocity",
    "_section_you_need_to",
    "_spark_bar",
    "stuck_task_count",
    "_task_cycle_minutes",
    "_worker_presence",
]
