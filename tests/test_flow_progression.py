"""Tests for flow progression — node_done, approve, reject, block, get_execution."""

from __future__ import annotations

import pytest

from pollypm.work.models import (
    Artifact,
    ArtifactKind,
    Decision,
    ExecutionStatus,
    OutputType,
    WorkOutput,
    WorkStatus,
)
from pollypm.work.sqlite_service import (
    InvalidTransitionError,
    SQLiteWorkService,
    ValidationError,
)


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def svc(tmp_path):
    db_path = tmp_path / "work.db"
    return SQLiteWorkService(db_path=db_path)


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


# ---------------------------------------------------------------------------
# node_done
# ---------------------------------------------------------------------------


class TestNodeDone:
    def test_node_done_advances_to_review(self, svc):
        task = _create_task(svc)
        claimed = _claim_task(svc, task)
        assert claimed.work_status == WorkStatus.IN_PROGRESS
        assert claimed.current_node_id == "implement"

        result = svc.node_done(task.task_id, "pete", _valid_work_output())
        assert result.work_status == WorkStatus.REVIEW
        assert result.current_node_id == "code_review"

        # The implement execution should be completed
        execs = svc.get_execution(task.task_id, node_id="implement")
        assert len(execs) == 1
        assert execs[0].status == ExecutionStatus.COMPLETED
        assert execs[0].completed_at is not None

        # A new code_review execution should be active
        review_execs = svc.get_execution(task.task_id, node_id="code_review")
        assert len(review_execs) == 1
        assert review_execs[0].status == ExecutionStatus.ACTIVE

    def test_node_done_without_work_output_rejected(self, svc):
        task = _create_task(svc)
        _claim_task(svc, task)

        with pytest.raises(ValidationError, match="Work output is required"):
            svc.node_done(task.task_id, "pete", None)

    def test_node_done_with_empty_artifacts_rejected(self, svc):
        task = _create_task(svc)
        _claim_task(svc, task)

        bad_output = WorkOutput(
            type=OutputType.CODE_CHANGE,
            summary="Did something",
            artifacts=[],
        )
        with pytest.raises(ValidationError, match="at least one artifact"):
            svc.node_done(task.task_id, "pete", bad_output)

    def test_node_done_wrong_actor_rejected(self, svc):
        task = _create_task(svc)
        _claim_task(svc, task)

        with pytest.raises(ValidationError, match="does not match role"):
            svc.node_done(task.task_id, "polly", _valid_work_output())

    def test_node_done_not_in_progress_rejected(self, svc):
        task = _create_task(svc)
        # Task is in draft state
        with pytest.raises(InvalidTransitionError, match="draft"):
            svc.node_done(task.task_id, "pete", _valid_work_output())


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------


class TestApprove:
    def test_approve_advances_to_done(self, svc):
        task = _create_task(svc)
        _claim_task(svc, task)
        svc.node_done(task.task_id, "pete", _valid_work_output())

        result = svc.approve(task.task_id, "polly")
        assert result.work_status == WorkStatus.DONE
        assert result.current_node_id is None

    def test_approve_wrong_actor_rejected(self, svc):
        task = _create_task(svc)
        _claim_task(svc, task)
        svc.node_done(task.task_id, "pete", _valid_work_output())

        with pytest.raises(ValidationError, match="does not match role"):
            svc.approve(task.task_id, "pete")

    def test_approve_not_in_review_rejected(self, svc):
        task = _create_task(svc)
        _claim_task(svc, task)
        # Task is in_progress, not review
        with pytest.raises(InvalidTransitionError, match="in_progress"):
            svc.approve(task.task_id, "polly")

    def test_approve_at_terminal_makes_done(self, svc):
        """Standard flow: after code_review approve, task is done."""
        task = _create_task(svc)
        _claim_task(svc, task)
        svc.node_done(task.task_id, "pete", _valid_work_output())
        result = svc.approve(task.task_id, "polly", reason="LGTM")

        assert result.work_status == WorkStatus.DONE

        # Check the review execution has approved decision
        review_execs = svc.get_execution(task.task_id, node_id="code_review")
        assert len(review_execs) == 1
        assert review_execs[0].decision == Decision.APPROVED
        assert review_execs[0].decision_reason == "LGTM"


# ---------------------------------------------------------------------------
# reject
# ---------------------------------------------------------------------------


class TestReject:
    def test_reject_loops_back(self, svc):
        task = _create_task(svc)
        _claim_task(svc, task)
        svc.node_done(task.task_id, "pete", _valid_work_output())

        result = svc.reject(task.task_id, "polly", "Needs more tests")
        assert result.work_status == WorkStatus.IN_PROGRESS
        assert result.current_node_id == "implement"

        # New execution at implement with visit=2
        impl_execs = svc.get_execution(task.task_id, node_id="implement")
        assert len(impl_execs) == 2
        assert impl_execs[0].visit == 1
        assert impl_execs[0].status == ExecutionStatus.COMPLETED
        assert impl_execs[1].visit == 2
        assert impl_execs[1].status == ExecutionStatus.ACTIVE

    def test_reject_without_reason_rejected(self, svc):
        task = _create_task(svc)
        _claim_task(svc, task)
        svc.node_done(task.task_id, "pete", _valid_work_output())

        with pytest.raises(ValidationError, match="Reason is required"):
            svc.reject(task.task_id, "polly", "")

    def test_full_rejection_cycle(self, svc):
        """implement(v1) -> review -> reject -> implement(v2) -> review -> approve -> done"""
        task = _create_task(svc)
        _claim_task(svc, task)

        # v1: implement -> review -> reject
        svc.node_done(task.task_id, "pete", _valid_work_output())
        svc.reject(task.task_id, "polly", "Needs work")

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
        # implement v1, code_review v1, implement v2, code_review v2(?)
        all_execs = svc.get_execution(task.task_id)
        assert len(all_execs) == 4

        impl_execs = svc.get_execution(task.task_id, node_id="implement")
        assert len(impl_execs) == 2
        assert impl_execs[0].visit == 1
        assert impl_execs[1].visit == 2

        review_execs = svc.get_execution(task.task_id, node_id="code_review")
        assert len(review_execs) == 2
        assert review_execs[0].visit == 1
        assert review_execs[0].decision == Decision.REJECTED
        assert review_execs[1].visit == 2
        assert review_execs[1].decision == Decision.APPROVED


# ---------------------------------------------------------------------------
# block
# ---------------------------------------------------------------------------


class TestBlock:
    def test_block_sets_status(self, svc):
        task = _create_task(svc)
        _claim_task(svc, task)

        blocker = _create_task(svc, title="Blocker task")

        result = svc.block(task.task_id, "pm", blocker.task_id)
        assert result.work_status == WorkStatus.BLOCKED

        # Execution should be blocked
        execs = svc.get_execution(task.task_id, node_id="implement")
        assert len(execs) == 1
        assert execs[0].status == ExecutionStatus.BLOCKED


# ---------------------------------------------------------------------------
# spike flow (no review)
# ---------------------------------------------------------------------------


class TestSpikeFlow:
    def test_spike_flow_no_review(self, svc):
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
    def test_execution_audit_trail(self, svc):
        """Full lifecycle with one rejection."""
        task = _create_task(svc)
        _claim_task(svc, task)
        svc.node_done(task.task_id, "pete", _valid_work_output())
        svc.reject(task.task_id, "polly", "Not good enough")
        svc.node_done(task.task_id, "pete", _valid_work_output())
        svc.approve(task.task_id, "polly")

        all_execs = svc.get_execution(task.task_id)
        # implement v1, code_review v1, implement v2, code_review v2
        assert len(all_execs) == 4

        # All should be completed
        for ex in all_execs:
            assert ex.status == ExecutionStatus.COMPLETED

        # Check decisions
        review_execs = [e for e in all_execs if e.node_id == "code_review"]
        assert review_execs[0].decision == Decision.REJECTED
        assert review_execs[0].decision_reason == "Not good enough"
        assert review_execs[1].decision == Decision.APPROVED

    def test_work_output_stored_on_execution(self, svc):
        task = _create_task(svc)
        _claim_task(svc, task)

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

        execs = svc.get_execution(task.task_id, node_id="implement")
        assert len(execs) == 1
        stored = execs[0].work_output
        assert stored is not None
        assert stored.type == OutputType.CODE_CHANGE
        assert stored.summary == "Built the feature"
        assert len(stored.artifacts) == 2
        assert stored.artifacts[0].kind == ArtifactKind.COMMIT
        assert stored.artifacts[0].ref == "sha123"
        assert stored.artifacts[1].path == "src/main.py"

    def test_get_execution_filters(self, svc):
        task = _create_task(svc)
        _claim_task(svc, task)
        svc.node_done(task.task_id, "pete", _valid_work_output())
        svc.reject(task.task_id, "polly", "Redo it")
        svc.node_done(task.task_id, "pete", _valid_work_output())
        svc.approve(task.task_id, "polly")

        # Filter by node_id
        impl_only = svc.get_execution(task.task_id, node_id="implement")
        assert len(impl_only) == 2

        review_only = svc.get_execution(task.task_id, node_id="code_review")
        assert len(review_only) == 2

        # Filter by visit
        visit1 = svc.get_execution(task.task_id, visit=1)
        assert all(e.visit == 1 for e in visit1)

        visit2 = svc.get_execution(task.task_id, visit=2)
        assert all(e.visit == 2 for e in visit2)

        # Filter by both
        impl_v2 = svc.get_execution(
            task.task_id, node_id="implement", visit=2
        )
        assert len(impl_v2) == 1
        assert impl_v2[0].node_id == "implement"
        assert impl_v2[0].visit == 2
