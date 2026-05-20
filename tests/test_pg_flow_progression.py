"""Pg re-coverage of flow progression (#1785).

Replaces the sqlite-bound ``tests/test_flow_progression.py`` that
Slice K-tests part 4 (#1786) deleted. Ports the backend-neutral parts
of the deleted suite — ``node_done``, ``approve``, ``reject``, ``block``,
spike flow, and ``get_execution`` filter coverage — against the
``pg_work_service`` fixture.

What stays sqlite-only / out of scope here
------------------------------------------

The deleted module also exercised the filesystem ``project_path`` git
auto-merge path (``approve`` against a real repo with task-branch
worktree, uncommitted-changes gate, ``.gitignore`` add/add union,
issues/ scaffold allow-list, itsalive scaffold allow-list, #946/#947
pre-stage + preserve-local-only logic). PgWorkService.approve still
flags ``resume_merge`` as Slice C ("git auto-merge is Slice C") so the
git-side gates haven't been ported to pg yet. Those tests are
intentionally not re-added here — they belong with the git-merge port
(see #1785 follow-up + the pg-side seam tracker).
"""

from __future__ import annotations

import pytest

from pollypm.rejection_feedback import (
    feedback_target_task_id,
    is_rejection_feedback_task,
    rejection_feedback_preview,
)
from pollypm.work.models import (
    Artifact,
    ArtifactKind,
    Decision,
    ExecutionStatus,
    OutputType,
    WorkOutput,
    WorkStatus,
)
from pollypm.work.service_support import (
    InvalidTransitionError,
    ValidationError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_task(svc, flow="standard", **kwargs):
    defaults = dict(
        title="Test task",
        description="A test task",
        type="task",
        project="proj",
        flow_template=flow,
        roles={"worker": "pete", "reviewer": "polly"},
        priority="normal",
        created_by="tester",
    )
    defaults.update(kwargs)
    return svc.create(**defaults)


def _create_spike_task(svc, **kwargs):
    defaults = dict(
        title="Spike task",
        description="Research something",
        type="spike",
        project="proj",
        flow_template="spike",
        roles={"worker": "pete"},
        priority="normal",
        created_by="tester",
    )
    defaults.update(kwargs)
    return svc.create(**defaults)


def _valid_work_output():
    return WorkOutput(
        type=OutputType.CODE_CHANGE,
        summary="Implemented the feature",
        artifacts=[
            Artifact(
                kind=ArtifactKind.COMMIT,
                description="feat: add new feature",
                ref="abc123",
            ),
        ],
    )


def _claim_task(svc, task):
    """Queue and claim a task, returning the claimed task."""
    svc.queue(task.task_id, "pm")
    return svc.claim(task.task_id, "pete")


def _current_work_node(svc, task):
    """Return the active work node id from the flow template."""
    flow = svc.get_flow(task.flow_template_id)
    for node_id, node in flow.nodes.items():
        if getattr(node.type, "value", node.type) == "work":
            return node_id
    raise AssertionError("no work node in flow")


# ---------------------------------------------------------------------------
# node_done
# ---------------------------------------------------------------------------


class TestNodeDone:
    def test_node_done_advances_to_review(self, pg_work_service):
        svc = pg_work_service
        task = _create_task(svc)
        claimed = _claim_task(svc, task)
        assert claimed.work_status == WorkStatus.IN_PROGRESS
        work_node = claimed.current_node_id

        result = svc.node_done(task.task_id, "pete", _valid_work_output())
        assert result.work_status == WorkStatus.REVIEW

        # The work execution should be completed
        execs = svc.get_execution(task.task_id, node_id=work_node)
        assert len(execs) == 1
        assert execs[0].status == ExecutionStatus.COMPLETED
        assert execs[0].completed_at is not None

        # A new review execution should be active
        review_node = result.current_node_id
        review_execs = svc.get_execution(task.task_id, node_id=review_node)
        assert len(review_execs) == 1
        assert review_execs[0].status == ExecutionStatus.ACTIVE

    def test_node_done_without_work_output_rejected(self, pg_work_service):
        svc = pg_work_service
        task = _create_task(svc)
        _claim_task(svc, task)

        # After wg03, the error message is longer (three-question rule)
        # but still mentions --output, which is the actionable fix.
        with pytest.raises(ValidationError, match="--output"):
            svc.node_done(task.task_id, "pete", None)

    def test_node_done_with_empty_artifacts_rejected(self, pg_work_service):
        svc = pg_work_service
        task = _create_task(svc)
        _claim_task(svc, task)

        bad_output = WorkOutput(
            type=OutputType.CODE_CHANGE,
            summary="Did something",
            artifacts=[],
        )
        with pytest.raises(ValidationError, match="at least one artifact"):
            svc.node_done(task.task_id, "pete", bad_output)

    def test_node_done_wrong_actor_rejected(self, pg_work_service):
        svc = pg_work_service
        task = _create_task(svc)
        _claim_task(svc, task)

        with pytest.raises(ValidationError, match="does not match role"):
            svc.node_done(task.task_id, "polly", _valid_work_output())

    def test_node_done_not_in_progress_rejected(self, pg_work_service):
        svc = pg_work_service
        task = _create_task(svc)
        # Task is in draft state — node_done can't run.
        with pytest.raises(InvalidTransitionError):
            svc.node_done(task.task_id, "pete", _valid_work_output())


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------


class TestApprove:
    def test_approve_advances_to_done(self, pg_work_service):
        svc = pg_work_service
        task = _create_task(svc)
        _claim_task(svc, task)
        svc.node_done(task.task_id, "pete", _valid_work_output())

        result = svc.approve(task.task_id, "polly")
        assert result.work_status == WorkStatus.DONE
        assert result.current_node_id is None

    def test_approve_wrong_actor_rejected(self, pg_work_service):
        svc = pg_work_service
        task = _create_task(svc)
        _claim_task(svc, task)
        svc.node_done(task.task_id, "pete", _valid_work_output())

        with pytest.raises(ValidationError, match="does not match role"):
            svc.approve(task.task_id, "pete")

    def test_approve_not_in_review_rejected(self, pg_work_service):
        svc = pg_work_service
        task = _create_task(svc)
        _claim_task(svc, task)
        # Task is in_progress, not review
        with pytest.raises(InvalidTransitionError):
            svc.approve(task.task_id, "polly")

    def test_approve_at_terminal_makes_done(self, pg_work_service):
        """Standard flow: after review approve, task is done."""
        svc = pg_work_service
        task = _create_task(svc)
        _claim_task(svc, task)
        done = svc.node_done(task.task_id, "pete", _valid_work_output())
        review_node = done.current_node_id

        result = svc.approve(task.task_id, "polly", reason="LGTM")
        assert result.work_status == WorkStatus.DONE

        # Check the review execution has approved decision
        review_execs = svc.get_execution(task.task_id, node_id=review_node)
        assert len(review_execs) == 1
        assert review_execs[0].decision == Decision.APPROVED
        assert review_execs[0].decision_reason == "LGTM"


# ---------------------------------------------------------------------------
# reject
# ---------------------------------------------------------------------------


class TestReject:
    def test_reject_loops_back_to_rework(self, pg_work_service):
        """#777 — reviewer rejection now lands the task in an explicit
        REWORK state. The rework node + assignee are still active so
        a worker can re-claim and continue."""
        svc = pg_work_service
        task = _create_task(svc)
        claimed = _claim_task(svc, task)
        work_node = claimed.current_node_id
        svc.node_done(task.task_id, "pete", _valid_work_output())

        result = svc.reject(task.task_id, "polly", "Needs more tests")
        assert result.work_status == WorkStatus.REWORK
        assert result.current_node_id == work_node

        # New execution at the work node with visit=2
        impl_execs = svc.get_execution(task.task_id, node_id=work_node)
        assert len(impl_execs) == 2
        assert impl_execs[0].visit == 1
        assert impl_execs[0].status == ExecutionStatus.COMPLETED
        assert impl_execs[1].visit == 2
        assert impl_execs[1].status == ExecutionStatus.ACTIVE

    def test_rework_can_advance_via_node_done(self, pg_work_service):
        """#777 — after rejection, the worker re-runs the work node and
        calls node_done. REWORK must be a valid source state for the
        node-done transition.
        """
        svc = pg_work_service
        task = _create_task(svc)
        _claim_task(svc, task)
        svc.node_done(task.task_id, "pete", _valid_work_output())
        rejected = svc.reject(task.task_id, "polly", "needs more tests")
        assert rejected.work_status == WorkStatus.REWORK

        re_done = svc.node_done(task.task_id, "pete", _valid_work_output())
        # Next node is review again, so status moves to REVIEW.
        assert re_done.work_status == WorkStatus.REVIEW

    def test_reject_without_reason_rejected(self, pg_work_service):
        svc = pg_work_service
        task = _create_task(svc)
        _claim_task(svc, task)
        svc.node_done(task.task_id, "pete", _valid_work_output())

        with pytest.raises(ValidationError, match="Reason is required"):
            svc.reject(task.task_id, "polly", "")

    def test_reject_creates_feedback_inbox_item(self, pg_work_service):
        svc = pg_work_service
        task = _create_task(svc)
        _claim_task(svc, task)
        svc.node_done(task.task_id, "pete", _valid_work_output())

        svc.reject(task.task_id, "polly", "Needs better rollback coverage")

        feedback_tasks = [
            candidate
            for candidate in svc.list_tasks(project="proj")
            if is_rejection_feedback_task(candidate)
        ]
        assert len(feedback_tasks) == 1
        feedback = feedback_tasks[0]
        assert feedback_target_task_id(feedback) == task.task_id
        assert (
            rejection_feedback_preview(feedback)
            == "Needs better rollback coverage"
        )

    def test_full_rejection_cycle(self, pg_work_service):
        """implement(v1) -> review -> reject -> implement(v2) -> review -> approve -> done"""
        svc = pg_work_service
        task = _create_task(svc)
        claimed = _claim_task(svc, task)
        work_node = claimed.current_node_id

        # v1: implement -> review -> reject
        svc.node_done(task.task_id, "pete", _valid_work_output())
        rejected = svc.reject(task.task_id, "polly", "Needs work")
        # capture review_node from the v1 cycle
        v1_review_execs = svc.get_execution(task.task_id)
        review_node = next(
            (
                row.node_id
                for row in v1_review_execs
                if row.node_id != work_node
            ),
            None,
        )
        assert review_node is not None
        assert rejected.work_status == WorkStatus.REWORK

        # v2: implement -> review -> approve
        wo2 = WorkOutput(
            type=OutputType.CODE_CHANGE,
            summary="Fixed the issues",
            artifacts=[
                Artifact(
                    kind=ArtifactKind.COMMIT,
                    description="fix: address review feedback",
                    ref="def456",
                ),
            ],
        )
        svc.node_done(task.task_id, "pete", wo2)
        result = svc.approve(task.task_id, "polly")

        assert result.work_status == WorkStatus.DONE

        # Should have 4 execution records total:
        # work v1, review v1, work v2, review v2
        all_execs = svc.get_execution(task.task_id)
        assert len(all_execs) == 4

        impl_execs = svc.get_execution(task.task_id, node_id=work_node)
        assert len(impl_execs) == 2
        assert impl_execs[0].visit == 1
        assert impl_execs[1].visit == 2

        review_execs = svc.get_execution(task.task_id, node_id=review_node)
        assert len(review_execs) == 2
        assert review_execs[0].visit == 1
        assert review_execs[0].decision == Decision.REJECTED
        assert review_execs[1].visit == 2
        assert review_execs[1].decision == Decision.APPROVED


# ---------------------------------------------------------------------------
# block
# ---------------------------------------------------------------------------


class TestBlock:
    def test_block_sets_status(self, pg_work_service):
        svc = pg_work_service
        task = _create_task(svc)
        claimed = _claim_task(svc, task)
        work_node = claimed.current_node_id

        blocker = _create_task(svc, title="Blocker task")

        result = svc.block(task.task_id, "pm", blocker.task_id)
        assert result.work_status == WorkStatus.BLOCKED

        # Execution should be blocked
        execs = svc.get_execution(task.task_id, node_id=work_node)
        assert len(execs) == 1
        assert execs[0].status == ExecutionStatus.BLOCKED

    def test_block_persists_dependency_row(self, pg_work_service):
        """block() must INSERT a blocks row so auto-unblock can find it
        when the blocker reaches done (issue #133)."""
        svc = pg_work_service
        task = _create_task(svc)
        _claim_task(svc, task)

        blocker = _create_task(svc, title="Blocker task")

        svc.block(task.task_id, "pm", blocker.task_id)

        # The dependency row is on the task — blocked_by should reflect it
        blocked = svc.get(task.task_id)
        assert (blocker.project, blocker.task_number) in blocked.blocked_by

        # dependents() from the blocker's side should list the blocked task
        deps = svc.dependents(blocker.task_id)
        assert any(d.task_id == task.task_id for d in deps)

    def test_block_then_blocker_done_auto_unblocks(self, pg_work_service):
        """After block(), marking the blocker done should auto-unblock the
        task via the auto-unblock cascade (issue #133)."""
        svc = pg_work_service
        task = _create_task(svc)
        _claim_task(svc, task)

        blocker = _create_task(svc, title="Blocker task")

        svc.block(task.task_id, "pm", blocker.task_id)
        assert svc.get(task.task_id).work_status == WorkStatus.BLOCKED

        # Move blocker to done — auto-unblock should fire.
        svc.mark_done(blocker.task_id, "agent-1")

        # Task was IN_PROGRESS before block; auto-unblock returns it to queued.
        unblocked = svc.get(task.task_id)
        assert unblocked.work_status == WorkStatus.QUEUED

    def test_block_fires_sync_adapters(self, pg_schema_pool):
        """block() must call _sync_transition so adapters see the blocked
        state (issue #136)."""
        from pollypm.work.pg_service import PgWorkService
        from pollypm.work.sync import SyncManager

        events: list[tuple[str, str, str]] = []

        class RecordingAdapter:
            name = "recorder"

            def on_create(self, task):
                events.append(("create", task.task_id, ""))

            def on_transition(self, task, old_status, new_status):
                events.append(("transition", old_status, new_status))

            def on_update(self, task, changed_fields):
                events.append(("update", task.task_id, ",".join(changed_fields)))

        mgr = SyncManager()
        mgr.register(RecordingAdapter())
        svc = PgWorkService(pool=pg_schema_pool, ro_pool=None, sync_manager=mgr)

        task = _create_task(svc)
        _claim_task(svc, task)
        blocker = _create_task(svc, title="Blocker task")

        events.clear()
        svc.block(task.task_id, "pm", blocker.task_id)

        # Must have fired a transition event with new_status == 'blocked'
        transition_events = [e for e in events if e[0] == "transition"]
        assert any(
            new == WorkStatus.BLOCKED.value for _, _, new in transition_events
        ), f"Expected blocked transition in {transition_events}"


# ---------------------------------------------------------------------------
# spike flow (no review)
# ---------------------------------------------------------------------------


class TestSpikeFlow:
    def test_spike_flow_no_review(self, pg_work_service):
        svc = pg_work_service
        task = _create_spike_task(svc)
        svc.queue(task.task_id, "pm")
        svc.claim(task.task_id, "pete")

        wo = WorkOutput(
            type=OutputType.DOCUMENT,
            summary="Research findings",
            artifacts=[
                Artifact(
                    kind=ArtifactKind.NOTE,
                    description="Found that X is better than Y",
                ),
            ],
        )
        result = svc.node_done(task.task_id, "pete", wo)
        assert result.work_status == WorkStatus.DONE
        assert result.current_node_id is None


# ---------------------------------------------------------------------------
# get_execution filters
# ---------------------------------------------------------------------------


class TestGetExecution:
    def test_execution_audit_trail(self, pg_work_service):
        """Full lifecycle with one rejection."""
        svc = pg_work_service
        task = _create_task(svc)
        _claim_task(svc, task)
        svc.node_done(task.task_id, "pete", _valid_work_output())
        svc.reject(task.task_id, "polly", "Not good enough")
        svc.node_done(task.task_id, "pete", _valid_work_output())
        svc.approve(task.task_id, "polly")

        all_execs = svc.get_execution(task.task_id)
        # work v1, review v1, work v2, review v2
        assert len(all_execs) == 4

        # All should be completed
        for ex in all_execs:
            assert ex.status == ExecutionStatus.COMPLETED

        # Check decisions — find the review-node executions by decision presence
        review_execs = [e for e in all_execs if e.decision is not None]
        assert len(review_execs) == 2
        review_execs.sort(key=lambda e: e.visit)
        assert review_execs[0].decision == Decision.REJECTED
        assert review_execs[0].decision_reason == "Not good enough"
        assert review_execs[1].decision == Decision.APPROVED

    def test_work_output_stored_on_execution(self, pg_work_service):
        svc = pg_work_service
        task = _create_task(svc)
        claimed = _claim_task(svc, task)
        work_node = claimed.current_node_id

        wo = WorkOutput(
            type=OutputType.CODE_CHANGE,
            summary="Built the feature",
            artifacts=[
                Artifact(
                    kind=ArtifactKind.COMMIT,
                    description="feat: the thing",
                    ref="sha123",
                ),
                Artifact(
                    kind=ArtifactKind.FILE_CHANGE,
                    description="Modified src/main.py",
                    path="src/main.py",
                ),
            ],
        )
        svc.node_done(task.task_id, "pete", wo)

        execs = svc.get_execution(task.task_id, node_id=work_node)
        assert len(execs) == 1
        stored = execs[0].work_output
        assert stored is not None
        assert stored.type == OutputType.CODE_CHANGE
        assert stored.summary == "Built the feature"
        assert len(stored.artifacts) == 2
        assert stored.artifacts[0].kind == ArtifactKind.COMMIT
        assert stored.artifacts[0].ref == "sha123"
        assert stored.artifacts[1].path == "src/main.py"

    def test_get_execution_filters(self, pg_work_service):
        svc = pg_work_service
        task = _create_task(svc)
        claimed = _claim_task(svc, task)
        work_node = claimed.current_node_id
        done = svc.node_done(task.task_id, "pete", _valid_work_output())
        review_node = done.current_node_id

        svc.reject(task.task_id, "polly", "Redo it")
        svc.node_done(task.task_id, "pete", _valid_work_output())
        svc.approve(task.task_id, "polly")

        # Filter by node_id
        impl_only = svc.get_execution(task.task_id, node_id=work_node)
        assert len(impl_only) == 2

        review_only = svc.get_execution(task.task_id, node_id=review_node)
        assert len(review_only) == 2

        # Filter by visit
        visit1 = svc.get_execution(task.task_id, visit=1)
        assert all(e.visit == 1 for e in visit1)

        visit2 = svc.get_execution(task.task_id, visit=2)
        assert all(e.visit == 2 for e in visit2)

        # Filter by both
        impl_v2 = svc.get_execution(
            task.task_id, node_id=work_node, visit=2
        )
        assert len(impl_v2) == 1
        assert impl_v2[0].node_id == work_node
        assert impl_v2[0].visit == 2
