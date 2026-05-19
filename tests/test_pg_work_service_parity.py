"""Parity tests: pg-backend port of the canonical work-service suite.

This file is the pg-backend twin of ``tests/test_work_service.py``
(issue #1737, Slice F). It exists so that as each ``PgWorkService``
method graduates from Slice-B-stub to real implementation across
Slices B-H, the corresponding sqlite test gets a mirrored pg test
that proves behavioural equivalence on the pg backend.

Why a separate file (and not a parametrize)
-------------------------------------------

The ``work_service`` dispatch fixture in ``tests/conftest.py`` does
support a ``@pytest.mark.backend('both')`` marker that runs a single
test against both backends. But applying that to the full ~50-test
sqlite suite today would just produce ~40 ``NotImplementedError``
failures because the pg surface is still being built. A dedicated
parity file lets us:

1. Track exactly which sqlite test classes have been mirrored.
2. Skip / xfail the slices that haven't shipped yet without
   contaminating the sqlite suite with markers.
3. Delete this file at Slice K cutover; ``test_work_service.py``
   gets repointed at pg via the dispatch default and the parity
   shim is no longer needed.

Coverage matrix (kept in sync with ``test_work_service.py``)
-----------------------------------------------------------

Implemented in Slice A (mirrored here):

* ``TestCreateTask`` — create, sequential ids, per-project ids,
  labels, acceptance criteria, full round-trip.
* ``TestGetTask`` — get, missing-raises.
* ``TestListTasks`` — by project, by status, by type, all.
* ``TestQueue`` (subset) — happy path only; the validation gates
  (description-required, requires_human_review) are Slice-B.
* ``TestCancel`` (subset) — happy path + reject-terminal.
* ``TestMarkDone`` (subset) — happy path + idempotency.
* ``TestListNonterminalTasks`` — Slice-A specific (no sqlite mirror).

Deferred — TODO when each slice lands:

* ``TestUpdateTask`` — Slice B (``update``).
* ``TestQueue`` validation paths — Slice B (gates).
* ``TestClaim`` — Slice B (worker session provisioning).
* ``TestHoldResume`` / ``TestBlock`` — Slice B (transition manager).
* ``TestTransitions`` — Slice B (audit emission).
* ``TestWorkerSessions`` — Slice B (worker session table).
* ``TestOwnerDerivation`` — Slice B (role derivation).
* ``TestAvailableFlowsProjectArg`` / ``TestFlowImmutability`` —
  Slice D (flow templates).
* ``TestActorTypeAgent`` / ``TestPlanTaskMetadata`` — Slice C
  (context entries + plan_version writers).

Tests use the ``pg_work_service`` fixture from ``tests/conftest_pg.py``;
the whole module skips if Docker / local pg with pgvector isn't
reachable.
"""

from __future__ import annotations

import pytest

from pollypm.work.models import (
    Priority,
    TaskType,
    WorkStatus,
)
from pollypm.work.service_support import (
    InvalidTransitionError,
    TaskNotFoundError,
)


# ---------------------------------------------------------------------------
# Helpers (mirror the ``_create_standard_task`` helper in the sqlite suite,
# but using ``"default"`` for the flow_template because Slice A doesn't
# load real flow definitions yet — Slice D ports the flow registry.)
# ---------------------------------------------------------------------------


def _create_pg_task(svc, project="proj", title="My task", description="Do the thing", **kwargs):
    """Create a task on the pg service with sensible defaults."""
    defaults = dict(
        title=title,
        description=description,
        type="task",
        project=project,
        flow_template="default",
        roles={"worker": "agent-1", "reviewer": "agent-2"},
        priority="normal",
        created_by="tester",
    )
    defaults.update(kwargs)
    return svc.create(**defaults)


# ---------------------------------------------------------------------------
# Task creation
# ---------------------------------------------------------------------------


class TestCreateTask:
    def test_create_task(self, pg_work_service):
        task = _create_pg_task(pg_work_service)
        assert task.project == "proj"
        assert task.task_number == 1
        assert task.title == "My task"
        assert task.description == "Do the thing"
        assert task.type is TaskType.TASK
        assert task.work_status is WorkStatus.DRAFT
        assert task.flow_template_id == "default"
        assert task.priority is Priority.NORMAL
        assert task.current_node_id is None
        assert task.assignee is None
        assert task.roles == {"worker": "agent-1", "reviewer": "agent-2"}
        assert task.created_at is not None
        assert task.created_by == "tester"

    def test_create_sequential_ids(self, pg_work_service):
        t1 = _create_pg_task(pg_work_service, title="First")
        t2 = _create_pg_task(pg_work_service, title="Second")
        assert t1.task_number == 1
        assert t2.task_number == 2

    def test_create_ids_per_project(self, pg_work_service):
        t1 = _create_pg_task(pg_work_service, project="alpha")
        t2 = _create_pg_task(pg_work_service, project="beta")
        assert t1.task_number == 1
        assert t2.task_number == 1

    def test_create_with_labels(self, pg_work_service):
        task = _create_pg_task(pg_work_service, labels=["bug", "urgent"])
        assert task.labels == ["bug", "urgent"]

    def test_create_with_acceptance_criteria(self, pg_work_service):
        task = _create_pg_task(pg_work_service, acceptance_criteria="Tests pass")
        assert task.acceptance_criteria == "Tests pass"

    @pytest.mark.skip(
        reason="Role validation lives in the gates layer; Slice B ports it."
    )
    def test_create_validates_roles(self, pg_work_service):
        ...

    @pytest.mark.skip(
        reason="Optional-role policy lives in the flow template; Slice D port."
    )
    def test_create_optional_role_not_required(self, pg_work_service):
        ...


# ---------------------------------------------------------------------------
# Task retrieval
# ---------------------------------------------------------------------------


class TestGetTask:
    def test_get_task(self, pg_work_service):
        created = _create_pg_task(pg_work_service)
        fetched = pg_work_service.get(f"{created.project}/{created.task_number}")
        assert fetched.title == created.title
        assert fetched.task_number == created.task_number
        assert fetched.work_status is WorkStatus.DRAFT

    def test_get_task_not_found(self, pg_work_service):
        with pytest.raises(TaskNotFoundError):
            pg_work_service.get("nonexistent/999")


# ---------------------------------------------------------------------------
# List tasks
# ---------------------------------------------------------------------------


class TestListTasks:
    def test_list_tasks_by_status(self, pg_work_service):
        t1 = _create_pg_task(pg_work_service, title="Draft task")
        t2 = _create_pg_task(pg_work_service, title="Queued task")
        pg_work_service.queue(f"{t2.project}/{t2.task_number}", actor="actor")

        drafts = pg_work_service.list_tasks(work_status="draft")
        assert len(drafts) == 1
        assert drafts[0].title == "Draft task"

        queued = pg_work_service.list_tasks(work_status="queued")
        assert len(queued) == 1
        assert queued[0].title == "Queued task"
        _ = t1  # silence unused

    def test_list_tasks_by_project(self, pg_work_service):
        _create_pg_task(pg_work_service, project="alpha")
        _create_pg_task(pg_work_service, project="beta")
        _create_pg_task(pg_work_service, project="alpha", title="Second alpha")

        alpha = pg_work_service.list_tasks(project="alpha")
        assert len(alpha) == 2

        beta = pg_work_service.list_tasks(project="beta")
        assert len(beta) == 1

    def test_list_tasks_all(self, pg_work_service):
        _create_pg_task(pg_work_service, title="A")
        _create_pg_task(pg_work_service, title="B")
        all_tasks = pg_work_service.list_tasks()
        assert len(all_tasks) == 2

    def test_list_tasks_by_type(self, pg_work_service):
        _create_pg_task(pg_work_service, type="task")
        _create_pg_task(pg_work_service, type="bug")
        tasks = pg_work_service.list_tasks(type="task")
        assert len(tasks) == 1


# ---------------------------------------------------------------------------
# Queue (happy path; validation gates ported in Slice B)
# ---------------------------------------------------------------------------


class TestQueue:
    def test_queue_from_draft(self, pg_work_service):
        task = _create_pg_task(pg_work_service, description="Ready to go")
        queued = pg_work_service.queue(
            f"{task.project}/{task.task_number}", actor="pm"
        )
        assert queued.work_status is WorkStatus.QUEUED

    def test_queue_is_idempotent(self, pg_work_service):
        task = _create_pg_task(pg_work_service, description="Ready")
        tid = f"{task.project}/{task.task_number}"
        pg_work_service.queue(tid, actor="pm")
        again = pg_work_service.queue(tid, actor="pm")
        assert again.work_status is WorkStatus.QUEUED

    @pytest.mark.skip(reason="description-required gate lands in Slice B.")
    def test_queue_without_description(self, pg_work_service):
        ...

    @pytest.mark.skip(
        reason="requires_human_review gate + skip_gates lands in Slice B."
    )
    def test_queue_requires_human_review_rejected(self, pg_work_service):
        ...


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


class TestCancel:
    def test_cancel_from_draft(self, pg_work_service):
        task = _create_pg_task(pg_work_service)
        tid = f"{task.project}/{task.task_number}"
        cancelled = pg_work_service.cancel(tid, actor="user", reason="moot")
        assert cancelled.work_status is WorkStatus.CANCELLED

    def test_cancel_rejects_terminal_task(self, pg_work_service):
        task = _create_pg_task(pg_work_service)
        tid = f"{task.project}/{task.task_number}"
        pg_work_service.mark_done(tid, actor="user")
        with pytest.raises(InvalidTransitionError):
            pg_work_service.cancel(tid, actor="user", reason="nope")


# ---------------------------------------------------------------------------
# Mark done (escape hatch — full ``node_done`` pipeline ports in Slice B)
# ---------------------------------------------------------------------------


class TestMarkDone:
    def test_mark_done_transitions_to_done(self, pg_work_service):
        task = _create_pg_task(pg_work_service)
        tid = f"{task.project}/{task.task_number}"
        done = pg_work_service.mark_done(tid, actor="user")
        assert done.work_status is WorkStatus.DONE

    def test_mark_done_is_idempotent(self, pg_work_service):
        task = _create_pg_task(pg_work_service)
        tid = f"{task.project}/{task.task_number}"
        pg_work_service.mark_done(tid, actor="user")
        again = pg_work_service.mark_done(tid, actor="user")
        assert again.work_status is WorkStatus.DONE


# ---------------------------------------------------------------------------
# Slice-A-specific: list_nonterminal_tasks
# ---------------------------------------------------------------------------


class TestListNonterminalTasks:
    def test_excludes_done_and_cancelled(self, pg_work_service):
        a = _create_pg_task(pg_work_service, title="a")
        b = _create_pg_task(pg_work_service, title="b")
        c = _create_pg_task(pg_work_service, title="c")
        pg_work_service.mark_done(f"{a.project}/{a.task_number}", actor="u")
        pg_work_service.cancel(
            f"{b.project}/{b.task_number}", actor="u", reason="r"
        )
        rows = pg_work_service.list_nonterminal_tasks(project="proj")
        assert [t.task_number for t in rows] == [c.task_number]


# ---------------------------------------------------------------------------
# Slice B+ port landed — ``claim`` / ``approve`` / ``node_done`` / ``update``
# now have real implementations on PgWorkService. The Slice A stub-raises
# tests that originally lived here are deleted because they tracked an
# obsolete pre-Slice-B contract; assertions about real semantics live in
# the test_pg_work_service_full.py / test_pg_work_service_parity.py suites.
# ---------------------------------------------------------------------------
