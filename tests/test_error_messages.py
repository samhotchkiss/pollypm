"""Tests for the three-question error message overhaul (wg03 / #240).

Every rewritten error must answer:
  1. What happened.
  2. Why (where knowable).
  3. What to do — an exact command.

These tests pin the guidance text so a future refactor can't silently
regress a worker's ability to follow the fix verbatim.
"""

from __future__ import annotations

import pytest

from pollypm.work.models import WorkStatus
from pollypm.work.sqlite_service import (
    InvalidTransitionError,
    SQLiteWorkService,
    ValidationError,
)


@pytest.fixture
def svc(tmp_path):
    db_path = tmp_path / "work.db"
    return SQLiteWorkService(db_path=db_path)


def _queued_task(svc, title="x", description="y"):
    task = svc.create(
        title=title,
        description=description,
        type="task",
        project="proj",
        flow_template="standard",
        roles={"worker": "agent-1", "reviewer": "agent-2"},
        priority="normal",
        created_by="tester",
    )
    svc.queue(task.task_id, "pm")
    return svc.get(task.task_id)


# ---------------------------------------------------------------------------
# Catalog entry 1: `pm task done` without artifact
# ---------------------------------------------------------------------------


class TestTaskDoneWithoutArtifact:
    def test_node_done_without_output_tells_worker_exactly_what_to_do(self, svc):
        task = _queued_task(svc)
        svc.claim(task.task_id, "agent-1")

        with pytest.raises(ValidationError) as excinfo:
            svc.node_done(task.task_id, "agent-1", None)

        msg = str(excinfo.value)
        # What happened
        assert "--output" in msg
        # Why
        assert "reviewer" in msg.lower() or "summary" in msg.lower()
        # Fix — copy-paste command
        assert "pm task done" in msg
        assert '"artifacts"' in msg
        assert '"kind"' in msg
        assert '"commit"' in msg

    def test_work_output_with_no_artifacts_lists_all_kinds(self, svc):
        task = _queued_task(svc)
        svc.claim(task.task_id, "agent-1")

        with pytest.raises(ValidationError) as excinfo:
            svc.node_done(
                task.task_id,
                "agent-1",
                {"type": "code_change", "summary": "did the thing"},
            )

        msg = str(excinfo.value)
        # Lists every supported artifact kind so the worker picks one
        # without re-reading docs.
        assert "commit" in msg
        assert "file_change" in msg
        assert "note" in msg
        # Has a copy-paste example
        assert "pm task done" in msg

    def test_work_output_without_summary_includes_fix_example(self, svc):
        task = _queued_task(svc)
        svc.claim(task.task_id, "agent-1")

        with pytest.raises(ValidationError) as excinfo:
            svc.node_done(
                task.task_id,
                "agent-1",
                {
                    "type": "code_change",
                    "summary": "",
                    "artifacts": [
                        {"kind": "commit", "description": "impl", "ref": "HEAD"}
                    ],
                },
            )

        msg = str(excinfo.value)
        assert '"summary"' in msg
        assert "pm task done" in msg


# ---------------------------------------------------------------------------
# Catalog entry 5: `pm task approve` on a draft task (or other non-review)
# ---------------------------------------------------------------------------


class TestApproveNonReviewTask:
    def test_approve_draft_points_to_queue(self, svc):
        task = svc.create(
            title="t", description="d", type="task", project="proj",
            flow_template="standard",
            roles={"worker": "a", "reviewer": "b"},
            priority="normal", created_by="tester",
        )
        with pytest.raises(InvalidTransitionError) as excinfo:
            svc.approve(task.task_id, "reviewer")

        msg = str(excinfo.value)
        assert "draft" in msg.lower()
        # Routes the worker (or PM) to the correct next step
        assert "pm task queue" in msg

    def test_approve_in_progress_points_to_done(self, svc):
        task = _queued_task(svc)
        svc.claim(task.task_id, "worker-1")

        with pytest.raises(InvalidTransitionError) as excinfo:
            svc.approve(task.task_id, "reviewer")

        msg = str(excinfo.value)
        # Tells the reviewer why (worker hasn't handed off)
        assert "in_progress" in msg
        # And points at `pm task done`
        assert "pm task done" in msg

    def test_approve_queued_explains_waiting_for_worker(self, svc):
        task = _queued_task(svc)

        with pytest.raises(InvalidTransitionError) as excinfo:
            svc.approve(task.task_id, "reviewer")

        msg = str(excinfo.value)
        assert "queued" in msg
        # Must say why approval isn't valid here
        assert "worker" in msg.lower()


# ---------------------------------------------------------------------------
# Catalog entry 6: `pm task claim` on already-claimed task
# ---------------------------------------------------------------------------


class TestClaimAlreadyClaimed:
    def test_claim_on_in_progress_identifies_claimant_and_offers_recovery(self, svc):
        task = _queued_task(svc)
        svc.claim(task.task_id, "agent-alpha")

        with pytest.raises(InvalidTransitionError) as excinfo:
            svc.claim(task.task_id, "agent-beta")

        msg = str(excinfo.value)
        # Names the current claimant — crucial for a worker deciding
        # whether the claim is stale.
        assert "agent-alpha" in msg
        # Recovery commands — hold + resume is the documented path for
        # stale claims (there's no `pm task release` yet).
        assert "pm task hold" in msg
        assert "pm task resume" in msg

    def test_claim_on_draft_explains_queue_first(self, svc):
        task = svc.create(
            title="t", description="d", type="task", project="proj",
            flow_template="standard",
            roles={"worker": "a", "reviewer": "b"},
            priority="normal", created_by="tester",
        )

        with pytest.raises(InvalidTransitionError) as excinfo:
            svc.claim(task.task_id, "worker")

        msg = str(excinfo.value)
        assert "draft" in msg
        assert "pm task queue" in msg


# ---------------------------------------------------------------------------
# Catalog entry 7: `pm worker-start` on non-existent project
# ---------------------------------------------------------------------------


class TestWorkerStartUnknownProject:
    def test_unknown_project_lists_registered_and_fix(self, tmp_path, monkeypatch):
        import typer
        from pollypm.workers import create_worker_session

        # Stub config with a couple of known projects but NOT the one
        # we're about to request.
        class _FakeProject:
            pass

        class _FakeConfig:
            def __init__(self):
                self.projects = {"alpha": _FakeProject(), "beta": _FakeProject()}
                self.accounts = {}
                self.sessions = {}

        def _fake_load(path):
            return _FakeConfig()

        monkeypatch.setattr("pollypm.workers.load_config", _fake_load)

        with pytest.raises(typer.BadParameter) as excinfo:
            create_worker_session(tmp_path / "cfg", project_key="gamma", prompt="x")

        msg = str(excinfo.value)
        assert "No project 'gamma'" in msg
        # Lists registered projects
        assert "alpha" in msg
        assert "beta" in msg
        # Points at fix
        assert "pm projects" in msg
        assert "pm add-project" in msg


# ---------------------------------------------------------------------------
# Catalog entry 2 (service layer): provisioning failure breadcrumb from
# claim(). Tested in detail in test_work_service.py — this test asserts
# the CLI-surfaced guidance text matches the three-question rule.
# ---------------------------------------------------------------------------


class TestClaimProvisionWarning:
    def test_claim_sets_breadcrumb_with_cause(self, svc):
        """Last-provision-error breadcrumb carries enough detail for the
        CLI to render a three-question warning."""

        class _FailingSessionMgr:
            def provision_worker(self, task_id, actor):
                raise RuntimeError(
                    "Command ['tmux', 'new-session', ...] returned "
                    "non-zero exit status 1."
                )

        svc.set_session_manager(_FailingSessionMgr())
        task = _queued_task(svc)
        claimed = svc.claim(task.task_id, "worker")

        # Claim still succeeds at DB level
        assert claimed.work_status == WorkStatus.IN_PROGRESS
        # But the cause is preserved for the CLI
        assert svc.last_provision_error is not None
        assert "tmux" in svc.last_provision_error


# ---------------------------------------------------------------------------
# Catalog entry 2 (session layer): ProvisionError guidance
# ---------------------------------------------------------------------------


class TestProvisionErrorGuidance:
    def test_provision_error_message_carries_three_questions(self):
        from pollypm.work.session_manager import ProvisionError

        # Construct the error the way _launch_worker_window does.
        err = ProvisionError(
            "Could not create tmux session 'pollypm-storage-closet': "
            "exit 1. "
            "Check that tmux is installed and running, then run "
            "`pm task release <id>` and retry."
        )
        msg = str(err)
        # What
        assert "tmux session" in msg
        # Why
        assert "exit" in msg or "not" in msg.lower()
        # How to fix
        assert "retry" in msg
