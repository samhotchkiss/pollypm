"""Tests for flow progression — node_done, approve, reject, block, get_execution."""

from __future__ import annotations

import subprocess

import pytest
from unittest.mock import MagicMock

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


def _git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def _git_stdout(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _create_review_task_on_git_repo(tmp_path):
    repo = _git_repo(tmp_path)
    svc = SQLiteWorkService(db_path=tmp_path / "work.db", project_path=repo)
    task = _create_task(svc)
    _claim_task(svc, task)

    current_branch = _git_stdout(repo, "rev-parse", "--abbrev-ref", "HEAD")
    task_branch = f"task/{task.project}-{task.task_number}"
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", task_branch], check=True)
    (repo / "feature.txt").write_text("done\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "feature.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "feat: worker change"], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", current_branch], check=True)

    svc.node_done(task.task_id, "pete", _valid_work_output())
    return repo, svc, task, task_branch


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

        # After wg03, the error message is longer (three-question rule)
        # but still mentions --output, which is the actionable fix.
        with pytest.raises(ValidationError, match="--output"):
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

    def test_approve_auto_merges_task_branch_into_repo(self, tmp_path):
        repo, svc, task, task_branch = _create_review_task_on_git_repo(tmp_path)

        result = svc.approve(task.task_id, "polly")

        assert result.work_status == WorkStatus.DONE
        assert (repo / "feature.txt").read_text(encoding="utf-8") == "done\n"
        merged = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", task_branch, "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert merged.returncode == 0

    def test_approve_refuses_auto_merge_when_repo_dirty(self, tmp_path):
        repo, svc, task, _task_branch = _create_review_task_on_git_repo(tmp_path)
        (repo / "README.md").write_text("dirty\n", encoding="utf-8")
        session_mgr = MagicMock()
        svc.set_session_manager(session_mgr)

        with pytest.raises(ValidationError, match="uncommitted changes"):
            svc.approve(task.task_id, "polly")

        assert svc.get(task.task_id).work_status == WorkStatus.REVIEW
        session_mgr.teardown_worker.assert_not_called()


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

    def test_reject_creates_feedback_inbox_item(self, svc):
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
        assert rejection_feedback_preview(feedback) == "Needs better rollback coverage"

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

    def test_block_persists_dependency_row(self, svc):
        """block() must INSERT a blocks row into work_task_dependencies so
        auto-unblock can find it when the blocker reaches done (issue #133)."""
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

    def test_block_then_blocker_done_auto_unblocks(self, svc):
        """After block(), marking the blocker done should auto-unblock the
        task via _check_auto_unblock (issue #133)."""
        task = _create_task(svc)
        _claim_task(svc, task)

        blocker = _create_task(svc, title="Blocker task")

        svc.block(task.task_id, "pm", blocker.task_id)
        assert svc.get(task.task_id).work_status == WorkStatus.BLOCKED

        # Move blocker to done — auto-unblock should fire
        svc.mark_done(blocker.task_id, "agent-1")

        # Task was IN_PROGRESS before block; auto-unblock returns it to queued.
        unblocked = svc.get(task.task_id)
        assert unblocked.work_status == WorkStatus.QUEUED

    def test_block_fires_sync_adapters(self, svc, tmp_path):
        """block() must call _sync_transition so adapters see the blocked
        state (issue #136)."""
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

        db_path = tmp_path / "sync.db"
        svc2 = SQLiteWorkService(db_path=db_path, sync_manager=mgr)

        task = _create_task(svc2)
        _claim_task(svc2, task)
        blocker = _create_task(svc2, title="Blocker task")

        events.clear()
        svc2.block(task.task_id, "pm", blocker.task_id)

        # Must have fired a transition event with new_status == 'blocked'
        transition_events = [e for e in events if e[0] == "transition"]
        assert any(
            new == WorkStatus.BLOCKED.value for _, _, new in transition_events
        ), f"Expected blocked transition in {transition_events}"


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
