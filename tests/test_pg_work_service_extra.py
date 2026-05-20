"""Gap-fill coverage for PgWorkService (#1794, Pattern B).

Re-adds the workflow / transition / audit-emission coverage that
Slice K-tests part 6 (#1795) deleted with ``tests/test_work_service.py``.
The pg parity suite in ``tests/test_pg_work_service_parity.py`` and the
Slice B coverage in ``tests/test_pg_work_service_full.py`` cover the
read+CRUD+single-state-transition surface, but the deleted module had
~50 tests and several of the higher-value contracts are still gaps:

* Full lifecycle transition audit trail (the ``TestTransitions`` class).
* Hold from queued / resume routing rules (in_progress vs queued).
* Cancel from in_progress / on_hold / terminal-rejected.
* Plan-task predecessor metadata + audit-event emission
  (``EVENT_PLAN_SUCCESSOR_CREATED``, ``EVENT_PLAN_VERSION_INCREMENTED``).
* Block validation: malformed blocker id rejected.

This module fills those gaps against ``pg_work_service``. The sqlite-
only seams (filesystem project_path flow resolution, ``_conn`` raw
SQL, ``_record_transition`` private enforcement) are out of scope —
PgWorkService doesn't expose those.
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
    ValidationError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_standard_task(
    svc,
    project: str = "proj",
    title: str = "My task",
    description: str = "Do the thing",
    **kwargs,
):
    defaults = dict(
        title=title,
        description=description,
        type="task",
        project=project,
        flow_template="standard",
        roles={"worker": "agent-1", "reviewer": "agent-2"},
        priority="normal",
        created_by="tester",
    )
    defaults.update(kwargs)
    return svc.create(**defaults)


# ---------------------------------------------------------------------------
# create — fields round-trip
# ---------------------------------------------------------------------------


class TestCreateFieldsRoundTrip:
    """A condensed version of the deleted ``TestCreateTask.test_create_task``
    smoke; the parity suite covers ids + per-project isolation but doesn't
    assert on every field of the round-trip."""

    def test_create_round_trips_all_fields(self, pg_work_service):
        task = _create_standard_task(pg_work_service)
        assert task.project == "proj"
        assert task.task_number == 1
        assert task.title == "My task"
        assert task.description == "Do the thing"
        assert task.type == TaskType.TASK
        assert task.work_status == WorkStatus.DRAFT
        assert task.flow_template_id == "standard"
        assert task.priority == Priority.NORMAL
        assert task.current_node_id is None
        assert task.assignee is None
        assert task.roles == {"worker": "agent-1", "reviewer": "agent-2"}
        assert task.created_at is not None
        assert task.created_by == "tester"


# ---------------------------------------------------------------------------
# Cancel — all source states
# ---------------------------------------------------------------------------


class TestCancel:
    def test_cancel_from_draft(self, pg_work_service):
        svc = pg_work_service
        task = _create_standard_task(svc)
        cancelled = svc.cancel(task.task_id, "pm", "not needed")
        assert cancelled.work_status == WorkStatus.CANCELLED

    def test_cancel_from_queued(self, pg_work_service):
        svc = pg_work_service
        task = _create_standard_task(svc, description="Queue it")
        svc.queue(task.task_id, "pm")
        cancelled = svc.cancel(task.task_id, "pm", "changed mind")
        assert cancelled.work_status == WorkStatus.CANCELLED

    def test_cancel_from_in_progress(self, pg_work_service):
        svc = pg_work_service
        task = _create_standard_task(svc, description="Claim it")
        svc.queue(task.task_id, "pm")
        svc.claim(task.task_id, "agent-1")
        cancelled = svc.cancel(task.task_id, "pm", "abort")
        assert cancelled.work_status == WorkStatus.CANCELLED

    def test_cancel_from_on_hold(self, pg_work_service):
        svc = pg_work_service
        task = _create_standard_task(svc, description="Hold it")
        svc.queue(task.task_id, "pm")
        svc.claim(task.task_id, "agent-1")
        svc.hold(task.task_id, "pm")
        cancelled = svc.cancel(task.task_id, "pm", "done waiting")
        assert cancelled.work_status == WorkStatus.CANCELLED

    def test_cancel_from_terminal_rejected(self, pg_work_service):
        svc = pg_work_service
        task = _create_standard_task(svc)
        svc.cancel(task.task_id, "pm", "bye")
        with pytest.raises(InvalidTransitionError):
            svc.cancel(task.task_id, "pm", "double cancel")


# ---------------------------------------------------------------------------
# Hold / Resume — source-state coverage + execution-aware routing
# ---------------------------------------------------------------------------


class TestHoldResume:
    def test_hold_from_in_progress(self, pg_work_service):
        svc = pg_work_service
        task = _create_standard_task(svc, description="Work")
        svc.queue(task.task_id, "pm")
        svc.claim(task.task_id, "agent-1")
        held = svc.hold(task.task_id, "pm", reason="waiting for info")
        assert held.work_status == WorkStatus.ON_HOLD

    def test_hold_from_queued(self, pg_work_service):
        svc = pg_work_service
        task = _create_standard_task(svc, description="Queued")
        svc.queue(task.task_id, "pm")
        held = svc.hold(task.task_id, "pm")
        assert held.work_status == WorkStatus.ON_HOLD

    def test_hold_from_wrong_state_rejected(self, pg_work_service):
        svc = pg_work_service
        task = _create_standard_task(svc)
        with pytest.raises(InvalidTransitionError):
            svc.hold(task.task_id, "pm")

    def test_resume_from_on_hold_with_active_execution(self, pg_work_service):
        """Resume goes to in_progress when a flow node is active
        (claim was issued before hold)."""
        svc = pg_work_service
        task = _create_standard_task(svc, description="Hold me")
        svc.queue(task.task_id, "pm")
        svc.claim(task.task_id, "agent-1")
        svc.hold(task.task_id, "pm")
        resumed = svc.resume(task.task_id, "pm")
        assert resumed.work_status == WorkStatus.IN_PROGRESS

    def test_resume_from_on_hold_without_execution(self, pg_work_service):
        """Resume goes to queued when no flow node is active
        (held while still in queued)."""
        svc = pg_work_service
        task = _create_standard_task(svc, description="Hold me")
        svc.queue(task.task_id, "pm")
        svc.hold(task.task_id, "pm")
        resumed = svc.resume(task.task_id, "pm")
        assert resumed.work_status == WorkStatus.QUEUED

    def test_resume_from_wrong_state_rejected(self, pg_work_service):
        svc = pg_work_service
        task = _create_standard_task(svc, description="Not on hold")
        svc.queue(task.task_id, "pm")
        with pytest.raises(InvalidTransitionError):
            svc.resume(task.task_id, "pm")


# ---------------------------------------------------------------------------
# Full lifecycle transition trail
# ---------------------------------------------------------------------------


class TestTransitions:
    def test_full_lifecycle_records_every_transition(self, pg_work_service):
        """Queue -> claim -> hold -> resume -> cancel."""
        svc = pg_work_service
        task = _create_standard_task(svc, description="Full lifecycle")
        tid = task.task_id

        svc.queue(tid, "pm")
        svc.claim(tid, "agent-1")
        svc.hold(tid, "pm", reason="waiting")
        svc.resume(tid, "pm")
        svc.cancel(tid, "pm", "done")

        final = svc.get(tid)
        states = [(t.from_state, t.to_state) for t in final.transitions]
        # The exact transitions: draft->queued->in_progress->on_hold->
        # in_progress->cancelled. PgWorkService should record all five.
        assert ("draft", "queued") in states
        assert ("queued", "in_progress") in states
        assert ("in_progress", "on_hold") in states
        assert ("on_hold", "in_progress") in states
        assert ("in_progress", "cancelled") in states
        assert len(final.transitions) >= 5

        # All transitions have timestamps and actors
        for t in final.transitions:
            assert t.timestamp is not None
            assert t.actor in ("pm", "agent-1")

        # Cancel transition has a reason
        cancel_tr = next(t for t in final.transitions if t.to_state == "cancelled")
        assert cancel_tr.reason == "done"


# ---------------------------------------------------------------------------
# Block — validation of blocker id format
# ---------------------------------------------------------------------------


class TestBlockValidation:
    def test_block_rejects_malformed_blocker_id(self, pg_work_service):
        svc = pg_work_service
        task = _create_standard_task(svc, description="Blocking target")
        svc.queue(task.task_id, "pm")
        svc.claim(task.task_id, "agent-1")

        with pytest.raises(ValidationError):
            svc.block(task.task_id, "pm", "bad-blocker-id")


# ---------------------------------------------------------------------------
# Plan-task metadata + audit-event emission (#1398)
# ---------------------------------------------------------------------------


class TestPlanTaskMetadata:
    def test_create_defaults_plan_version_to_1(self, pg_work_service):
        task = _create_standard_task(pg_work_service)
        assert task.plan_version == 1
        assert task.predecessor_task_id is None

    def test_create_with_predecessor_records_link(self, pg_work_service):
        svc = pg_work_service
        first = _create_standard_task(svc, title="original")
        successor = _create_standard_task(
            svc,
            title="replan",
            predecessor_task_id=first.task_id,
        )
        assert successor.predecessor_task_id == first.task_id
        # Re-read to confirm persistence.
        refetched = svc.get(successor.task_id)
        assert refetched.predecessor_task_id == first.task_id

    def test_list_successors_returns_replan_chain(self, pg_work_service):
        svc = pg_work_service
        first = _create_standard_task(svc, title="original")
        s1 = _create_standard_task(
            svc, title="replan-1", predecessor_task_id=first.task_id
        )
        s2 = _create_standard_task(
            svc, title="replan-2", predecessor_task_id=first.task_id
        )
        # Unrelated task should NOT appear.
        _create_standard_task(svc, title="unrelated")

        successors = svc.list_successors(first.task_id)
        ids = {s.task_id for s in successors}
        assert ids == {s1.task_id, s2.task_id}

    def test_increment_plan_version_bumps_value(self, pg_work_service):
        svc = pg_work_service
        task = _create_standard_task(svc)
        assert task.plan_version == 1
        bumped = svc.increment_plan_version(task.task_id, actor="architect")
        assert bumped.plan_version == 2
        refetched = svc.get(task.task_id)
        assert refetched.plan_version == 2

    def test_increment_plan_version_unknown_task_raises(self, pg_work_service):
        with pytest.raises(TaskNotFoundError):
            pg_work_service.increment_plan_version("ghost/999")

    def test_create_with_malformed_predecessor_raises(self, pg_work_service):
        svc = pg_work_service
        with pytest.raises(ValidationError):
            _create_standard_task(
                svc,
                title="bad replan",
                predecessor_task_id="not-a-task-id",
            )

    def test_increment_plan_version_emits_audit_event(
        self, pg_work_service, tmp_path, monkeypatch
    ):
        """``plan.version_incremented`` should fire with old/new versions."""
        from pollypm.audit import read_events
        from pollypm.audit.log import EVENT_PLAN_VERSION_INCREMENTED

        # Redirect the central audit tail to a temp dir so the test never
        # touches the user's real ~/.pollypm/audit/ tree.
        audit_home = tmp_path / "audit-home"
        monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))

        task = _create_standard_task(pg_work_service, project="auditproj")
        pg_work_service.increment_plan_version(
            task.task_id, actor="architect", reason="refined plan"
        )

        events = read_events(
            "auditproj", event=EVENT_PLAN_VERSION_INCREMENTED
        )
        assert len(events) == 1, (
            f"expected one plan.version_incremented event, "
            f"got: {[e.event for e in events]}"
        )
        evt = events[0]
        assert evt.subject == task.task_id
        assert evt.actor == "architect"
        assert evt.metadata["old_version"] == 1
        assert evt.metadata["new_version"] == 2
        assert evt.metadata["task_id"] == task.task_id
        assert evt.metadata.get("reason") == "refined plan"

    def test_create_successor_emits_plan_successor_audit_event(
        self, pg_work_service, tmp_path, monkeypatch
    ):
        from pollypm.audit import read_events
        from pollypm.audit.log import EVENT_PLAN_SUCCESSOR_CREATED

        audit_home = tmp_path / "audit-home"
        monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))

        svc = pg_work_service
        first = _create_standard_task(svc, project="replanproj")
        successor = _create_standard_task(
            svc,
            project="replanproj",
            title="replan",
            predecessor_task_id=first.task_id,
        )

        events = read_events(
            "replanproj", event=EVENT_PLAN_SUCCESSOR_CREATED
        )
        assert len(events) == 1
        evt = events[0]
        assert evt.subject == successor.task_id
        assert evt.metadata["predecessor"] == first.task_id
        assert evt.metadata["successor"] == successor.task_id

    def test_create_no_predecessor_does_not_emit_successor_event(
        self, pg_work_service, tmp_path, monkeypatch
    ):
        """A regular task create must NOT emit ``plan.successor_created``."""
        from pollypm.audit import read_events
        from pollypm.audit.log import EVENT_PLAN_SUCCESSOR_CREATED

        audit_home = tmp_path / "audit-home"
        monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))

        _create_standard_task(pg_work_service, project="noreplan")
        events = read_events(
            "noreplan", event=EVENT_PLAN_SUCCESSOR_CREATED
        )
        assert events == []
