"""Cycle 135 — perf: signature-based render skip on project dashboard.

The 10s refresh tick on ``PollyProjectDashboardApp`` previously did
a full Textual re-paint every tick even when nothing the user could
see had changed. Mirror the inbox loader's #752 pattern: skip the
re-paint when the signature is unchanged.

These tests pin the signature contract so structural changes
invalidate (forcing a re-paint) and pure no-ops do not.
"""

from __future__ import annotations

from pollypm.cockpit_ui import (
    ProjectDashboardData,
    _project_dashboard_signature,
)


def _make(**overrides) -> ProjectDashboardData:
    base = dict(
        project_key="demo",
        project_name="Demo",
        project_path=None,
        persona_name=None,
        pm_label="PM: Polly",
        exists_on_disk=True,
        status_dot="●",
        status_color="#3ddc84",
        status_label="active",
        active_worker=None,
        architect=None,
        task_counts={"in_progress": 1},
        task_buckets={},
        plan_path=None,
        plan_sections=[],
        plan_explainer=None,
        plan_text=None,
        plan_aux_files=[],
        plan_mtime=None,
        plan_stale_reason=None,
        activity_entries=[],
        inbox_count=0,
        inbox_top=[],
        action_items=[],
        alert_count=0,
    )
    base.update(overrides)
    return ProjectDashboardData(**base)


def test_signature_stable_for_identical_state() -> None:
    a = _make()
    b = _make()
    assert _project_dashboard_signature(a) == _project_dashboard_signature(b)


def test_signature_changes_on_task_count_change() -> None:
    a = _make(task_counts={"in_progress": 1})
    b = _make(task_counts={"in_progress": 2})
    assert _project_dashboard_signature(a) != _project_dashboard_signature(b)


def test_signature_changes_on_inbox_count_change() -> None:
    a = _make(inbox_count=0)
    b = _make(inbox_count=1)
    assert _project_dashboard_signature(a) != _project_dashboard_signature(b)


def test_signature_changes_on_status_label_change() -> None:
    a = _make(status_label="active")
    b = _make(status_label="idle")
    assert _project_dashboard_signature(a) != _project_dashboard_signature(b)


def test_signature_changes_on_pm_label_change() -> None:
    a = _make(pm_label="PM: Bea")
    b = _make(pm_label="PM: Archie")
    assert _project_dashboard_signature(a) != _project_dashboard_signature(b)


def test_signature_changes_on_worker_heartbeat() -> None:
    """Heartbeat updates intentionally invalidate so the worker
    section stays current."""
    a = _make(active_worker={"session_name": "w", "role": "worker", "last_heartbeat": "T1"})
    b = _make(active_worker={"session_name": "w", "role": "worker", "last_heartbeat": "T2"})
    assert _project_dashboard_signature(a) != _project_dashboard_signature(b)


def test_signature_changes_on_plan_mtime() -> None:
    a = _make(plan_mtime=100.0)
    b = _make(plan_mtime=200.0)
    assert _project_dashboard_signature(a) != _project_dashboard_signature(b)


def test_signature_changes_on_action_items_added() -> None:
    a = _make(action_items=[])
    b = _make(action_items=[{"task_id": "demo/1"}])
    assert _project_dashboard_signature(a) != _project_dashboard_signature(b)


def test_signature_handles_none_data() -> None:
    """The wrapper must not crash on a None data snapshot — the
    helper returns a stable sentinel so the comparison is well-
    defined during the load-error window."""
    assert _project_dashboard_signature(None) == _project_dashboard_signature(None)
    assert _project_dashboard_signature(None) != _project_dashboard_signature(_make())
