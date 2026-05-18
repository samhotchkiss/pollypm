"""Project-dashboard data snapshot used by the cockpit project drilldown.

Contract:
- Inputs: ~30 keyword arguments describing one project's state — identity
  (``project_key``, ``project_name``, ``project_path``, ``persona_name``,
  ``pm_label``, ``pm_persona``, ``exists_on_disk``), status indicators
  (``status_dot``, ``status_color``, ``status_label``), workers
  (``active_worker``, ``architect``), task aggregates (``task_counts``,
  ``task_buckets``), plan state (``plan_path``, ``plan_sections``,
  ``plan_explainer``, ``plan_text``, ``plan_aux_files``, ``plan_mtime``,
  ``plan_stale_reason``, ``plan_task_summary``), activity / inbox /
  action-item rows (``activity_entries``, ``inbox_count``, ``inbox_top``,
  ``action_items``), and alert summary (``alert_count``, ``alert_types``,
  ``enforce_plan``).
- Outputs: a ``ProjectDashboardData`` instance with each attribute
  declared via ``__slots__`` for memory + typo discipline.
- Side effects: none. Pure container.
- Invariants: this module owns one data class and nothing else; it has
  no dependency on Textual, cockpit state, or services. Keep it
  data-only — no rendering — so tests can poke individual attributes
  without mounting a Textual screen.
- Allowed dependencies: stdlib only (``pathlib.Path`` for type hints).
- Public: ``ProjectDashboardData`` is re-exported via ``cockpit_ui`` for
  back-compat (see #1354).

Wedge of the cockpit_ui.py god-module split tracked by #1354.
"""

from __future__ import annotations

from pathlib import Path


class ProjectDashboardData:
    """Snapshot of everything the dashboard renders — cached per tick.

    Constructed off the UI thread via ``_gather_project_dashboard``;
    the dashboard app holds the resulting object and reads fields for
    each section. Keep this *data-only* — no rendering — so tests can
    poke individual attributes without mounting a Textual screen.
    """

    __slots__ = (
        "project_key",
        "project_name",
        "project_path",
        "persona_name",
        "pm_persona",
        "pm_label",
        "exists_on_disk",
        "status_dot",
        "status_color",
        "status_label",
        "active_worker",
        "architect",
        "task_counts",
        "task_buckets",
        "plan_path",
        "plan_sections",
        "plan_explainer",
        "plan_text",
        "plan_aux_files",
        "plan_mtime",
        "plan_stale_reason",
        "plan_task_summary",
        "activity_entries",
        "inbox_count",
        "inbox_top",
        "action_items",
        "alert_count",
        "alert_types",
        "enforce_plan",
    )

    def __init__(
        self,
        *,
        project_key: str,
        project_name: str,
        project_path: Path | None,
        persona_name: str | None,
        pm_label: str,
        pm_persona: str | None = None,
        exists_on_disk: bool,
        status_dot: str,
        status_color: str,
        status_label: str,
        active_worker: dict | None,
        architect: dict | None,
        task_counts: dict[str, int],
        task_buckets: dict[str, list[dict]],
        plan_path: Path | None,
        plan_sections: list[str],
        plan_explainer: Path | None,
        plan_text: str | None,
        plan_aux_files: list[Path],
        plan_mtime: float | None,
        plan_stale_reason: str | None,
        activity_entries: list[dict],
        plan_task_summary: dict | None = None,
        inbox_count: int,
        inbox_top: list[dict],
        action_items: list[dict],
        alert_count: int,
        alert_types: list[str] | None = None,
        enforce_plan: bool = True,
    ) -> None:
        self.project_key = project_key
        self.project_name = project_name
        self.project_path = project_path
        self.persona_name = persona_name
        self.pm_persona = pm_persona
        self.pm_label = pm_label
        self.exists_on_disk = exists_on_disk
        self.status_dot = status_dot
        self.status_color = status_color
        self.status_label = status_label
        self.active_worker = active_worker
        self.architect = architect
        self.task_counts = task_counts
        self.task_buckets = task_buckets
        self.plan_path = plan_path
        self.plan_sections = plan_sections
        self.plan_explainer = plan_explainer
        self.plan_text = plan_text
        self.plan_aux_files = plan_aux_files
        self.plan_mtime = plan_mtime
        self.plan_stale_reason = plan_stale_reason
        self.plan_task_summary = plan_task_summary
        self.activity_entries = activity_entries
        self.inbox_count = inbox_count
        self.inbox_top = inbox_top
        self.action_items = action_items
        self.alert_count = alert_count
        self.alert_types = list(alert_types) if alert_types else []
        self.enforce_plan = enforce_plan


__all__ = ["ProjectDashboardData"]
