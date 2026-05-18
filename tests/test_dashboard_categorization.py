"""Categorization unit tests (#1572).

Pins the contracts the operator dashboard + rail glyph share:

* ``categorize_project`` returns the right ``ProjectState`` for each
  combination of inbox items / live workers / in-flight tasks, with
  WAITING winning over WORKING in the collision case (the load-bearing
  invariant from the issue spec).
* ``why_waiting`` maps every documented kind to its user-facing copy
  and honours the documented priority ordering when multiple items
  are present.
* ``what_working`` picks the most-recent worker on multi-worker
  projects and degrades gracefully when no worker is bound.
* ``glyph_for_project_state`` returns the documented ``◆ ● ○ ⏸``
  vocabulary — the rail and the dashboard import this same function.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pollypm.dashboard import (
    ProjectState,
    build_operator_dashboard_view,
    categorize_project,
    glyph_for_project_state,
    what_working,
    why_waiting,
)
from pollypm.inbox.kind import InboxItemKind


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


def _item(*, kind, project="demo", title="", scope=""):
    return SimpleNamespace(
        kind=kind, project=project, scope=scope, title=title,
    )


def _worker(*, project="demo", task_number=1, agent_name="claude", started_at="2026-05-17T10:00:00"):
    return SimpleNamespace(
        task_project=project,
        task_number=task_number,
        agent_name=agent_name,
        started_at=started_at,
    )


def _task(*, project="demo", title="t", status="in_progress", task_id=None):
    return SimpleNamespace(
        project=project,
        title=title,
        work_status=SimpleNamespace(value=status),
        task_id=task_id or f"{project}/1",
    )


class FakeWorkService:
    def __init__(self, *, workers=None, tasks=None, raise_on_workers=False, raise_on_tasks=False):
        self.workers = workers or []
        self.tasks = tasks or []
        self.raise_on_workers = raise_on_workers
        self.raise_on_tasks = raise_on_tasks
        self._by_id = {getattr(t, "task_id", f"{t.project}/1"): t for t in self.tasks}

    def list_worker_sessions(self, *, project=None, active_only=True):
        if self.raise_on_workers:
            raise RuntimeError("boom")
        out = []
        for w in self.workers:
            if project is not None and getattr(w, "task_project", None) != project:
                continue
            out.append(w)
        return out

    def list_tasks(self, *, project=None, **_kwargs):
        if self.raise_on_tasks:
            raise RuntimeError("boom")
        out = []
        for t in self.tasks:
            if project is not None and getattr(t, "project", None) != project:
                continue
            out.append(t)
        return out

    def get(self, task_id):
        try:
            return self._by_id[task_id]
        except KeyError as exc:
            raise LookupError(task_id) from exc


# ---------------------------------------------------------------------------
# glyph_for_project_state
# ---------------------------------------------------------------------------


def test_glyph_vocabulary_matches_spec() -> None:
    assert glyph_for_project_state(ProjectState.WAITING) == "◆"
    assert glyph_for_project_state(ProjectState.WORKING) == "●"
    assert glyph_for_project_state(ProjectState.IDLE) == "○"
    assert glyph_for_project_state(ProjectState.PAUSED) == "⏸"


# ---------------------------------------------------------------------------
# categorize_project
# ---------------------------------------------------------------------------


def test_categorize_waiting_when_item_awaits_user() -> None:
    svc = FakeWorkService()
    items = [_item(kind=InboxItemKind.APPROVAL_REQUEST)]
    assert categorize_project("demo", work_service=svc, inbox_items=items) is ProjectState.WAITING


def test_categorize_waiting_wins_over_working_collision() -> None:
    """Load-bearing invariant: Waiting beats Working when both apply."""
    svc = FakeWorkService(
        workers=[_worker()],
        tasks=[_task(status="in_progress")],
    )
    items = [_item(kind=InboxItemKind.PLAN_REVIEW_PENDING)]
    assert categorize_project("demo", work_service=svc, inbox_items=items) is ProjectState.WAITING


def test_categorize_working_when_live_worker() -> None:
    svc = FakeWorkService(workers=[_worker()])
    assert categorize_project("demo", work_service=svc) is ProjectState.WORKING


def test_categorize_working_when_task_in_progress() -> None:
    svc = FakeWorkService(tasks=[_task(status="in_progress")])
    assert categorize_project("demo", work_service=svc) is ProjectState.WORKING


def test_categorize_working_when_task_in_rework() -> None:
    svc = FakeWorkService(tasks=[_task(status="rework")])
    assert categorize_project("demo", work_service=svc) is ProjectState.WORKING


def test_categorize_idle_when_nothing_active() -> None:
    svc = FakeWorkService(tasks=[_task(status="done")])
    assert categorize_project("demo", work_service=svc) is ProjectState.IDLE


def test_categorize_waiting_when_task_on_hold() -> None:
    """#1542 — ``on_hold`` tasks surface as WAITING so the rail glyph
    matches the dashboard's ``◆ needs attention`` banner. A paused root
    task is blocking downstream work — the user owes a resume-or-cancel
    decision even if no inbox item tracks it.
    """
    svc = FakeWorkService(tasks=[_task(status="on_hold")])
    assert categorize_project("demo", work_service=svc) is ProjectState.WAITING


def test_categorize_waiting_when_on_hold_with_background_worker() -> None:
    """#1542 — the on_hold priority must outrank a live background
    worker, mirroring ``_dashboard_status``'s pill priority. Without
    this the rail would paint ``●`` while the dashboard pill paints
    ``◆ needs attention``.
    """
    svc = FakeWorkService(
        workers=[_worker()],
        tasks=[_task(status="on_hold")],
    )
    assert categorize_project("demo", work_service=svc) is ProjectState.WAITING


def test_categorize_idle_when_only_review_status_tasks() -> None:
    """``review`` parked tasks don't count as Working (or Waiting via
    the on_hold short-circuit). They surface via the inbox approval
    items instead.
    """
    svc = FakeWorkService(tasks=[_task(status="review")])
    assert categorize_project("demo", work_service=svc) is ProjectState.IDLE


def test_categorize_paused_when_untracked_and_quiet() -> None:
    svc = FakeWorkService()
    assert categorize_project("demo", work_service=svc, tracked=False) is ProjectState.PAUSED


def test_paused_with_waiting_items_surfaces_as_waiting() -> None:
    """Per cycle-87 asymmetry: paused projects with items still surface."""
    svc = FakeWorkService()
    items = [_item(kind=InboxItemKind.PLAN_REVIEW_PENDING)]
    state = categorize_project("demo", work_service=svc, inbox_items=items, tracked=False)
    assert state is ProjectState.WAITING


def test_legacy_kind_treated_as_waiting() -> None:
    """The predicate's fail-open contract: LEGACY items count as awaits_user."""
    svc = FakeWorkService()
    items = [_item(kind=InboxItemKind.LEGACY)]
    assert categorize_project("demo", work_service=svc, inbox_items=items) is ProjectState.WAITING


def test_informational_kinds_do_not_trigger_waiting() -> None:
    svc = FakeWorkService()
    items = [
        _item(kind=InboxItemKind.COMPLETION_FYI),
        _item(kind=InboxItemKind.INFO),
        _item(kind=InboxItemKind.ACTIVITY_EVENT),
        _item(kind=InboxItemKind.SELF_BUG_REPORT),
    ]
    assert categorize_project("demo", work_service=svc, inbox_items=items) is ProjectState.IDLE


def test_items_for_other_project_do_not_leak() -> None:
    svc = FakeWorkService()
    items = [_item(kind=InboxItemKind.APPROVAL_REQUEST, project="other")]
    assert categorize_project("demo", work_service=svc, inbox_items=items) is ProjectState.IDLE


def test_work_service_failures_degrade_to_idle() -> None:
    svc = FakeWorkService(raise_on_workers=True, raise_on_tasks=True)
    assert categorize_project("demo", work_service=svc) is ProjectState.IDLE


def test_scope_used_when_project_field_blank() -> None:
    svc = FakeWorkService()
    items = [_item(kind=InboxItemKind.APPROVAL_REQUEST, project="", scope="demo")]
    assert categorize_project("demo", work_service=svc, inbox_items=items) is ProjectState.WAITING


# ---------------------------------------------------------------------------
# why_waiting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind,expected",
    [
        (InboxItemKind.PLAN_REVIEW_PENDING, "Needs your review of the new plan"),
        (InboxItemKind.APPROVAL_REQUEST, "Needs your approval"),
        (InboxItemKind.PM_QUESTION_UNANSWERED, "PM is waiting on your reply"),
        (InboxItemKind.MANUAL_DECISION, "Polly needs your decision"),
    ],
)
def test_why_waiting_static_copy(kind, expected) -> None:
    assert why_waiting([_item(kind=kind)]) == expected


def test_why_waiting_watchdog_interpolates_subject() -> None:
    item = _item(kind=InboxItemKind.WATCHDOG_OPERATOR_DISPATCH, title="stuck on auth")
    assert why_waiting([item]) == "Watchdog escalated: stuck on auth"


def test_why_waiting_watchdog_no_subject_uses_fallback() -> None:
    item = _item(kind=InboxItemKind.WATCHDOG_OPERATOR_DISPATCH, title="")
    assert why_waiting([item]) == "Watchdog escalation"


def test_why_waiting_priority_ordering() -> None:
    """plan_review > approval > watchdog > pm_question > manual_decision."""
    items = [
        _item(kind=InboxItemKind.MANUAL_DECISION, title="manual"),
        _item(kind=InboxItemKind.APPROVAL_REQUEST, title="approve"),
        _item(kind=InboxItemKind.PLAN_REVIEW_PENDING, title="plan"),
        _item(kind=InboxItemKind.PM_QUESTION_UNANSWERED, title="pm"),
        _item(kind=InboxItemKind.WATCHDOG_OPERATOR_DISPATCH, title="wd"),
    ]
    assert why_waiting(items) == "Needs your review of the new plan"

    # Drop plan_review — approval wins.
    items_without_plan = [i for i in items if i.kind is not InboxItemKind.PLAN_REVIEW_PENDING]
    assert why_waiting(items_without_plan) == "Needs your approval"

    # Drop plan_review + approval — watchdog wins.
    remaining = [
        i for i in items
        if i.kind not in {InboxItemKind.PLAN_REVIEW_PENDING, InboxItemKind.APPROVAL_REQUEST}
    ]
    assert why_waiting(remaining) == "Watchdog escalated: wd"


def test_why_waiting_legacy_fallback() -> None:
    assert why_waiting([_item(kind=InboxItemKind.LEGACY)]) == "Needs your attention"
    assert why_waiting([]) == "Needs your attention"


# ---------------------------------------------------------------------------
# what_working
# ---------------------------------------------------------------------------


def test_what_working_single_worker() -> None:
    svc = FakeWorkService(
        workers=[_worker(agent_name="claude", task_number=7)],
        tasks=[_task(title="rebuild auth", task_id="demo/7")],
    )
    assert what_working("demo", work_service=svc) == "claude: rebuild auth"


def test_what_working_picks_most_recent_worker() -> None:
    svc = FakeWorkService(
        workers=[
            _worker(agent_name="alpha", task_number=1, started_at="2026-05-15T10:00:00"),
            _worker(agent_name="beta", task_number=2, started_at="2026-05-17T11:00:00"),
            _worker(agent_name="gamma", task_number=3, started_at="2026-05-16T10:00:00"),
        ],
        tasks=[
            _task(title="t-alpha", task_id="demo/1"),
            _task(title="t-beta", task_id="demo/2"),
            _task(title="t-gamma", task_id="demo/3"),
        ],
    )
    assert what_working("demo", work_service=svc) == "beta: t-beta"


def test_what_working_no_worker_falls_back_to_status() -> None:
    svc = FakeWorkService(tasks=[_task(status="in_progress", title="refactor")])
    assert what_working("demo", work_service=svc) == "in progress: refactor"


def test_what_working_no_data_yields_active_fallback() -> None:
    svc = FakeWorkService()
    assert what_working("demo", work_service=svc) == "Active"


def test_what_working_truncates_long_lines() -> None:
    title = "x" * 200
    svc = FakeWorkService(
        workers=[_worker(agent_name="claude", task_number=1)],
        tasks=[_task(title=title, task_id="demo/1")],
    )
    rendered = what_working("demo", work_service=svc)
    assert len(rendered) <= 80
    assert rendered.endswith("…")


# ---------------------------------------------------------------------------
# build_operator_dashboard_view — integration over the in-memory fixture
# ---------------------------------------------------------------------------


def test_build_view_places_projects_in_one_section_each() -> None:
    """Same fixture, single pass, mutually-exclusive sections."""
    svc = FakeWorkService(
        workers=[_worker(project="working_proj", agent_name="claude", task_number=1)],
        tasks=[
            _task(project="working_proj", title="ship feature", task_id="working_proj/1"),
            _task(project="idle_proj", status="done", task_id="idle_proj/1"),
        ],
    )
    inbox = {
        "waiting_proj": [_item(kind=InboxItemKind.APPROVAL_REQUEST, project="waiting_proj")],
    }
    view = build_operator_dashboard_view(
        ["waiting_proj", "working_proj", "idle_proj", "paused_proj"],
        work_service=svc,
        inbox_items_by_project=inbox,
        tracked_by_project={"paused_proj": False},
    )

    assert [r.project_key for r in view.waiting] == ["waiting_proj"]
    assert [r.project_key for r in view.working] == ["working_proj"]
    assert [r.project_key for r in view.idle] == ["idle_proj"]
    assert [r.project_key for r in view.paused] == ["paused_proj"]

    waiting_row = view.waiting[0]
    assert waiting_row.glyph == glyph_for_project_state(ProjectState.WAITING)
    assert waiting_row.detail == "Needs your approval"

    working_row = view.working[0]
    assert working_row.glyph == glyph_for_project_state(ProjectState.WORKING)
    assert working_row.detail == "claude: ship feature"


def test_build_view_empty_input_returns_empty_view() -> None:
    svc = FakeWorkService()
    view = build_operator_dashboard_view([], work_service=svc)
    assert view.waiting == ()
    assert view.working == ()
    assert view.idle == ()
    assert view.paused == ()
