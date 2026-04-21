"""Tests for SQLiteWorkService — task CRUD and state transitions."""

from __future__ import annotations

import pytest

from pollypm.work.models import (
    ExecutionStatus,
    Priority,
    TaskType,
    WorkStatus,
)
from pollypm.work.sqlite_service import (
    InvalidTransitionError,
    SQLiteWorkService,
    TaskNotFoundError,
    ValidationError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def svc(tmp_path):
    """Create a fresh SQLiteWorkService with an in-memory-equivalent temp DB."""
    db_path = tmp_path / "work.db"
    return SQLiteWorkService(db_path=db_path)


def _create_standard_task(svc, project="proj", title="My task", description="Do the thing", **kwargs):
    """Helper to create a task with the standard flow and valid roles."""
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


def _create_critique_task(
    svc,
    *,
    project="proj",
    title="Critique",
    critic="critic_simplicity",
    description="Review the candidate plan",
    **kwargs,
):
    """Helper to create a planner critic task on the ``critique_flow``."""
    defaults = dict(
        title=title,
        description=description,
        type="task",
        project=project,
        flow_template="critique_flow",
        roles={"critic": critic, "requester": "architect"},
        priority="high",
        created_by="architect",
    )
    defaults.update(kwargs)
    return svc.create(**defaults)


# ---------------------------------------------------------------------------
# Task creation
# ---------------------------------------------------------------------------


class TestCreateTask:
    def test_create_task(self, svc):
        task = _create_standard_task(svc)
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

    def test_create_validates_roles(self, svc):
        with pytest.raises(ValidationError, match="Required role 'worker'"):
            svc.create(
                title="Missing roles",
                type="task",
                project="proj",
                flow_template="standard",
                roles={"reviewer": "agent-2"},  # missing 'worker'
                priority="normal",
            )

    def test_create_optional_role_not_required(self, svc):
        """The 'requester' role is optional in the standard flow."""
        task = svc.create(
            title="No requester",
            description="Fine without requester",
            type="task",
            project="proj",
            flow_template="standard",
            roles={"worker": "agent-1", "reviewer": "agent-2"},
            priority="normal",
        )
        assert task.task_number == 1

    def test_create_sequential_ids(self, svc):
        t1 = _create_standard_task(svc, title="First")
        t2 = _create_standard_task(svc, title="Second")
        assert t1.task_number == 1
        assert t2.task_number == 2

    def test_create_ids_per_project(self, svc):
        t1 = _create_standard_task(svc, project="alpha")
        t2 = _create_standard_task(svc, project="beta")
        assert t1.task_number == 1
        assert t2.task_number == 1

    def test_create_with_labels(self, svc):
        task = _create_standard_task(svc, labels=["bug", "urgent"])
        assert task.labels == ["bug", "urgent"]

    def test_create_with_acceptance_criteria(self, svc):
        task = _create_standard_task(svc, acceptance_criteria="Tests pass")
        assert task.acceptance_criteria == "Tests pass"


# ---------------------------------------------------------------------------
# Task retrieval
# ---------------------------------------------------------------------------


class TestGetTask:
    def test_get_task(self, svc):
        created = _create_standard_task(svc)
        fetched = svc.get(f"{created.project}/{created.task_number}")
        assert fetched.title == created.title
        assert fetched.task_number == created.task_number
        assert fetched.work_status == WorkStatus.DRAFT

    def test_get_task_not_found(self, svc):
        with pytest.raises(TaskNotFoundError):
            svc.get("nonexistent/999")


# ---------------------------------------------------------------------------
# List tasks
# ---------------------------------------------------------------------------


class TestListTasks:
    def test_list_tasks_by_status(self, svc):
        t1 = _create_standard_task(svc, title="Draft task")
        t2 = _create_standard_task(svc, title="Queued task", description="Has description")
        svc.queue(t2.task_id, "actor")

        drafts = svc.list_tasks(work_status="draft")
        assert len(drafts) == 1
        assert drafts[0].title == "Draft task"

        queued = svc.list_tasks(work_status="queued")
        assert len(queued) == 1
        assert queued[0].title == "Queued task"

    def test_list_tasks_by_project(self, svc):
        _create_standard_task(svc, project="alpha")
        _create_standard_task(svc, project="beta")
        _create_standard_task(svc, project="alpha", title="Second alpha")

        alpha = svc.list_tasks(project="alpha")
        assert len(alpha) == 2

        beta = svc.list_tasks(project="beta")
        assert len(beta) == 1

    def test_list_tasks_all(self, svc):
        _create_standard_task(svc, title="A")
        _create_standard_task(svc, title="B")
        all_tasks = svc.list_tasks()
        assert len(all_tasks) == 2

    def test_list_tasks_by_type(self, svc):
        _create_standard_task(svc, type="task")
        _create_standard_task(svc, type="bug")
        tasks = svc.list_tasks(type="task")
        assert len(tasks) == 1


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


class TestUpdateTask:
    def test_update_task(self, svc):
        task = _create_standard_task(svc)
        updated = svc.update(task.task_id, title="New title", description="New desc")
        assert updated.title == "New title"
        assert updated.description == "New desc"

    def test_update_labels(self, svc):
        task = _create_standard_task(svc)
        updated = svc.update(task.task_id, labels=["a", "b"])
        assert updated.labels == ["a", "b"]

    def test_update_roles(self, svc):
        task = _create_standard_task(svc)
        updated = svc.update(
            task.task_id, roles={"worker": "new-agent", "reviewer": "agent-2"}
        )
        assert updated.roles["worker"] == "new-agent"

    def test_update_cannot_change_status(self, svc):
        task = _create_standard_task(svc)
        with pytest.raises(ValidationError, match="Cannot change work_status"):
            svc.update(task.task_id, work_status="queued")

    def test_update_cannot_change_flow(self, svc):
        task = _create_standard_task(svc)
        with pytest.raises(ValidationError, match="Cannot change flow_template"):
            svc.update(task.task_id, flow_template_id="spike")

    def test_update_not_found(self, svc):
        with pytest.raises(TaskNotFoundError):
            svc.update("nope/1", title="x")


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


class TestQueue:
    def test_queue_from_draft(self, svc):
        task = _create_standard_task(svc, description="Ready to go")
        queued = svc.queue(task.task_id, "pm")
        assert queued.work_status == WorkStatus.QUEUED

    def test_queue_without_description(self, svc):
        task = _create_standard_task(svc, description="")
        with pytest.raises(ValidationError, match="description"):
            svc.queue(task.task_id, "pm")

    def test_queue_from_wrong_state(self, svc):
        task = _create_standard_task(svc, description="Ready")
        svc.queue(task.task_id, "pm")
        svc.claim(task.task_id, "agent-1")
        with pytest.raises(InvalidTransitionError, match="in_progress"):
            svc.queue(task.task_id, "pm")

    def test_queue_requires_human_review_rejected(self, svc):
        task = _create_standard_task(
            svc, description="Needs approval", requires_human_review=True
        )
        with pytest.raises(InvalidTransitionError, match="human review"):
            svc.queue(task.task_id, "pm")

    def test_queue_requires_human_review_bypass_with_skip_gates(self, svc):
        """skip_gates=True is a stopgap bypass until inbox integration
        lands (issue #135) — otherwise requires_human_review tasks are
        permanently stuck in draft."""
        task = _create_standard_task(
            svc, description="Needs approval", requires_human_review=True
        )
        queued = svc.queue(task.task_id, "pm", skip_gates=True)
        assert queued.work_status == WorkStatus.QUEUED


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------


class TestClaim:
    def test_claim_from_queued(self, svc):
        task = _create_standard_task(svc, description="Work to do")
        svc.queue(task.task_id, "pm")
        claimed = svc.claim(task.task_id, "agent-1")

        assert claimed.work_status == WorkStatus.IN_PROGRESS
        assert claimed.assignee == "agent-1"
        assert claimed.current_node_id == "implement"  # standard flow start

        # Should have an execution record
        assert len(claimed.executions) == 1
        ex = claimed.executions[0]
        assert ex.node_id == "implement"
        assert ex.visit == 1
        assert ex.status == ExecutionStatus.ACTIVE
        assert ex.started_at is not None

    def test_claim_from_wrong_state(self, svc):
        task = _create_standard_task(svc, description="Not queued yet")
        with pytest.raises(InvalidTransitionError, match="draft"):
            svc.claim(task.task_id, "agent-1")

    def test_claim_is_atomic(self, svc):
        """On claim failure, neither assignee nor status should change."""
        task = _create_standard_task(svc, description="Draft only")
        # Task is in draft, claim should fail
        try:
            svc.claim(task.task_id, "agent-1")
        except InvalidTransitionError:
            pass
        reloaded = svc.get(task.task_id)
        assert reloaded.work_status == WorkStatus.DRAFT
        assert reloaded.assignee is None

    def test_claim_records_provision_error_breadcrumb(self, svc):
        """#243: when a session manager raises during provisioning,
        the error must be stashed on the service so the CLI can show
        an actionable warning instead of a silent success."""

        class _FailingSessionMgr:
            def provision_worker(self, task_id, actor):
                raise RuntimeError("tmux server unreachable")

        svc.set_session_manager(_FailingSessionMgr())

        task = _create_standard_task(svc, description="Work to do")
        svc.queue(task.task_id, "pm")

        assert svc.last_provision_error is None
        claimed = svc.claim(task.task_id, "agent-1")

        # Claim succeeded at the DB level...
        assert claimed.work_status == WorkStatus.IN_PROGRESS
        # ...but the error is surfaced so the CLI can flag it.
        assert svc.last_provision_error is not None
        assert "tmux server unreachable" in svc.last_provision_error

    def test_claim_clears_stale_provision_error(self, svc):
        """A successful provision after a previous failure must clear
        the breadcrumb so a follow-up claim doesn't show a stale
        warning."""
        calls = {"n": 0}

        class _FlakeySessionMgr:
            def provision_worker(self, task_id, actor):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("first attempt fails")
                return object()  # succeeds on retry

        svc.set_session_manager(_FlakeySessionMgr())

        t1 = _create_standard_task(svc, description="first")
        svc.queue(t1.task_id, "pm")
        svc.claim(t1.task_id, "agent-1")
        assert svc.last_provision_error is not None

        t2 = _create_standard_task(svc, description="second")
        svc.queue(t2.task_id, "pm")
        svc.claim(t2.task_id, "agent-1")
        # Breadcrumb cleared because the second provision succeeded.
        assert svc.last_provision_error is None


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


class TestCancel:
    def test_cancel_from_draft(self, svc):
        task = _create_standard_task(svc)
        cancelled = svc.cancel(task.task_id, "pm", "not needed")
        assert cancelled.work_status == WorkStatus.CANCELLED

    def test_cancel_from_queued(self, svc):
        task = _create_standard_task(svc, description="Queue it")
        svc.queue(task.task_id, "pm")
        cancelled = svc.cancel(task.task_id, "pm", "changed mind")
        assert cancelled.work_status == WorkStatus.CANCELLED

    def test_cancel_from_in_progress(self, svc):
        task = _create_standard_task(svc, description="Claim it")
        svc.queue(task.task_id, "pm")
        svc.claim(task.task_id, "agent-1")
        cancelled = svc.cancel(task.task_id, "pm", "abort")
        assert cancelled.work_status == WorkStatus.CANCELLED

    def test_cancel_from_on_hold(self, svc):
        task = _create_standard_task(svc, description="Hold it")
        svc.queue(task.task_id, "pm")
        svc.claim(task.task_id, "agent-1")
        svc.hold(task.task_id, "pm")
        cancelled = svc.cancel(task.task_id, "pm", "done waiting")
        assert cancelled.work_status == WorkStatus.CANCELLED

    def test_cancel_from_terminal(self, svc):
        task = _create_standard_task(svc)
        svc.cancel(task.task_id, "pm", "bye")
        with pytest.raises(InvalidTransitionError, match="terminal"):
            svc.cancel(task.task_id, "pm", "double cancel")

    def test_cancel_prunes_critique_child_tasks_and_reuses_numbering(self, svc):
        plan = svc.create(
            title="Plan project demo",
            description="Run the planner.",
            type="task",
            project="demo",
            flow_template="plan_project",
            roles={"architect": "architect"},
            priority="high",
            created_by="tester",
        )
        critic_names = (
            "critic_simplicity",
            "critic_maintainability",
            "critic_user",
            "critic_operational",
            "critic_security",
        )
        critic_ids: list[str] = []
        for critic in critic_names:
            task = _create_critique_task(
                svc,
                project="demo",
                title=f"{critic} panel review",
                critic=critic,
            )
            svc.link(plan.task_id, task.task_id, "parent")
            critic_ids.append(task.task_id)

        for task_id in critic_ids:
            cancelled = svc.cancel(task_id, "architect", "critic output collected")
            assert cancelled.work_status == WorkStatus.CANCELLED
            with pytest.raises(TaskNotFoundError):
                svc.get(task_id)

        remaining = [task.task_id for task in svc.list_tasks(project="demo")]
        assert remaining == [plan.task_id]
        assert svc.get(plan.task_id).children == []

        implementation = _create_standard_task(
            svc,
            project="demo",
            title="Implement module",
            description="Build the first module.",
        )
        assert implementation.task_id == "demo/2"

    def test_cancel_keeps_standalone_critique_flow_tasks(self, svc):
        critic = _create_critique_task(svc)

        cancelled = svc.cancel(critic.task_id, "architect", "no longer needed")

        assert cancelled.work_status == WorkStatus.CANCELLED
        assert svc.get(critic.task_id).work_status == WorkStatus.CANCELLED


# ---------------------------------------------------------------------------
# Hold / Resume
# ---------------------------------------------------------------------------


class TestHoldResume:
    def test_hold_from_in_progress(self, svc):
        task = _create_standard_task(svc, description="Work")
        svc.queue(task.task_id, "pm")
        svc.claim(task.task_id, "agent-1")
        held = svc.hold(task.task_id, "pm", "waiting for info")
        assert held.work_status == WorkStatus.ON_HOLD

    def test_hold_from_queued(self, svc):
        task = _create_standard_task(svc, description="Queued")
        svc.queue(task.task_id, "pm")
        held = svc.hold(task.task_id, "pm")
        assert held.work_status == WorkStatus.ON_HOLD

    def test_hold_from_wrong_state(self, svc):
        task = _create_standard_task(svc)
        with pytest.raises(InvalidTransitionError, match="draft"):
            svc.hold(task.task_id, "pm")

    def test_resume_from_on_hold_with_active_execution(self, svc):
        """Resume goes to in_progress when a flow node is active."""
        task = _create_standard_task(svc, description="Hold me")
        svc.queue(task.task_id, "pm")
        svc.claim(task.task_id, "agent-1")
        svc.hold(task.task_id, "pm")
        resumed = svc.resume(task.task_id, "pm")
        assert resumed.work_status == WorkStatus.IN_PROGRESS

    def test_resume_from_on_hold_without_execution(self, svc):
        """Resume goes to queued when no flow node is active."""
        task = _create_standard_task(svc, description="Hold me")
        svc.queue(task.task_id, "pm")
        svc.hold(task.task_id, "pm")
        resumed = svc.resume(task.task_id, "pm")
        assert resumed.work_status == WorkStatus.QUEUED

    def test_resume_from_wrong_state(self, svc):
        task = _create_standard_task(svc, description="Not on hold")
        svc.queue(task.task_id, "pm")
        with pytest.raises(InvalidTransitionError, match="queued"):
            svc.resume(task.task_id, "pm")


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


class TestTransitions:
    def test_transitions_recorded(self, svc):
        """Full lifecycle should record every transition."""
        task = _create_standard_task(svc, description="Full lifecycle")
        tid = task.task_id

        svc.queue(tid, "pm")
        svc.claim(tid, "agent-1")
        svc.hold(tid, "pm", "waiting")
        svc.resume(tid, "pm")
        svc.cancel(tid, "pm", "done")

        final = svc.get(tid)
        assert len(final.transitions) == 5

        states = [(t.from_state, t.to_state) for t in final.transitions]
        assert states == [
            ("draft", "queued"),
            ("queued", "in_progress"),
            ("in_progress", "on_hold"),
            ("on_hold", "in_progress"),
            ("in_progress", "cancelled"),
        ]

        # All transitions have timestamps and actors
        for t in final.transitions:
            assert t.timestamp is not None
            assert t.actor in ("pm", "agent-1")

        # Cancel transition has a reason
        assert final.transitions[-1].reason == "done"


class TestBlock:
    def test_block_rejects_malformed_blocker_id(self, svc):
        task = _create_standard_task(svc, description="Blocking target")
        svc.queue(task.task_id, "pm")
        svc.claim(task.task_id, "agent-1")

        with pytest.raises(ValidationError, match="project/number"):
            svc.block(task.task_id, "pm", "bad-blocker-id")


class TestWorkerSessions:
    def test_worker_session_round_trip(self, svc):
        task = _create_standard_task(svc, description="Session-backed task")
        svc.ensure_worker_session_schema()

        svc.upsert_worker_session(
            task_project=task.project,
            task_number=task.task_number,
            agent_name="agent-1",
            pane_id="pane-1",
            worktree_path="/tmp/worktree",
            branch_name="branch-1",
            started_at="2026-04-21T00:00:00+00:00",
        )

        record = svc.get_worker_session(
            task_project=task.project,
            task_number=task.task_number,
        )
        assert record is not None
        assert record.agent_name == "agent-1"
        assert record.branch_name == "branch-1"

        active = svc.list_worker_sessions(project=task.project)
        assert len(active) == 1
        assert active[0].task_number == task.task_number

        svc.end_worker_session(
            task_project=task.project,
            task_number=task.task_number,
            ended_at="2026-04-21T01:00:00+00:00",
            total_input_tokens=11,
            total_output_tokens=22,
            archive_path="/tmp/archive.tar.gz",
        )

        ended = svc.get_worker_session(
            task_project=task.project,
            task_number=task.task_number,
        )
        assert ended is not None
        assert ended.ended_at == "2026-04-21T01:00:00+00:00"
        assert ended.total_input_tokens == 11
        assert ended.total_output_tokens == 22


# ---------------------------------------------------------------------------
# Owner derivation
# ---------------------------------------------------------------------------


class TestOwnerDerivation:
    def test_owner_draft(self, svc):
        """Draft tasks are owned by the project manager."""
        task = _create_standard_task(svc)
        owner = svc.derive_owner(task)
        assert owner == "project_manager"

    def test_owner_in_progress(self, svc):
        """In-progress task at implement node: owner is the worker role."""
        task = _create_standard_task(svc, description="Work")
        svc.queue(task.task_id, "pm")
        claimed = svc.claim(task.task_id, "agent-1")
        owner = svc.derive_owner(claimed)
        # The implement node has actor_type=role, actor_role=worker
        # roles["worker"] = "agent-1"
        assert owner == "agent-1"

    def test_owner_queued(self, svc):
        """Queued tasks have no current_node_id, so owner is None."""
        task = _create_standard_task(svc, description="Queue me")
        queued = svc.queue(task.task_id, "pm")
        owner = svc.derive_owner(queued)
        assert owner is None


# ---------------------------------------------------------------------------
# available_flows() / get_flow() — project arg honored
# ---------------------------------------------------------------------------


class TestAvailableFlowsProjectArg:
    def test_available_flows_uses_project_path_arg(self, tmp_path):
        """Passing project= to available_flows should pick up that project's
        project-local flows, not only the constructor-bound project (#146)."""
        import textwrap

        # Service is constructed with a project_path that has NO custom flows
        svc_db = tmp_path / "svc.db"
        svc = SQLiteWorkService(db_path=svc_db, project_path=tmp_path)

        # Create a second project directory with a custom flow
        other = tmp_path / "other_project"
        (other / ".pollypm" / "flows").mkdir(parents=True)
        (other / ".pollypm" / "flows" / "zebra.yaml").write_text(textwrap.dedent("""\
            name: zebra
            description: zebra flow
            roles:
              worker:
                description: w
            nodes:
              step:
                type: work
                actor_type: role
                actor_role: worker
                next_node: done
              done:
                type: terminal
            start_node: step
        """))

        # Without project arg: only the constructor's project, no zebra.
        base = {t.name for t in svc.available_flows()}
        assert "zebra" not in base

        # With an explicit path-like project (falls through to candidate path):
        scoped = {t.name for t in svc.available_flows(project=str(other))}
        assert "zebra" in scoped

    def test_get_flow_uses_project_path_arg(self, tmp_path):
        """get_flow should resolve a project-local override when project= is
        an explicit path, independent of the constructor-bound project."""
        import textwrap

        svc_db = tmp_path / "svc.db"
        svc = SQLiteWorkService(db_path=svc_db, project_path=tmp_path)

        other = tmp_path / "other_project"
        (other / ".pollypm" / "flows").mkdir(parents=True)
        (other / ".pollypm" / "flows" / "standard.yaml").write_text(textwrap.dedent("""\
            name: standard
            description: custom-for-other
            roles:
              worker:
                description: w
            nodes:
              step:
                type: work
                actor_type: role
                actor_role: worker
                next_node: done
              done:
                type: terminal
            start_node: step
        """))

        scoped = svc.get_flow("standard", project=str(other))
        assert scoped.description == "custom-for-other"


# ---------------------------------------------------------------------------
# Flow immutability (OQ-6) — tasks pin to their creation-time version
# ---------------------------------------------------------------------------


class TestFlowImmutability:
    """Tasks execute on the flow template version they were created with.

    Editing a flow YAML must not silently reroute in-flight tasks through
    the new graph. New tasks pick up the latest version; existing tasks
    stay on their pinned version (issue #134).
    """

    def _write_custom_standard(self, root, *, description):
        """Write a project-local override of the 'standard' flow."""
        import textwrap

        flows_dir = root / ".pollypm" / "flows"
        flows_dir.mkdir(parents=True, exist_ok=True)
        (flows_dir / "standard.yaml").write_text(textwrap.dedent(f"""\
            name: standard
            description: {description}
            roles:
              worker:
                description: Implements
              reviewer:
                description: Reviews
            nodes:
              implement:
                type: work
                actor_type: role
                actor_role: worker
                next_node: code_review
                gates: [has_assignee]
              code_review:
                type: review
                actor_type: role
                actor_role: reviewer
                next_node: done
                reject_node: implement
                gates: [has_work_output]
              done:
                type: terminal
            start_node: implement
        """))

    def _make_task(self, svc):
        return svc.create(
            title="Task",
            description="Do it",
            type="task",
            project="proj",
            flow_template="standard",
            roles={"worker": "pete", "reviewer": "polly"},
            priority="normal",
            created_by="tester",
        )

    def test_new_task_records_flow_template_version(self, tmp_path):
        """A freshly-created task exposes its pinned flow_template_version."""
        self._write_custom_standard(tmp_path, description="v1-body")
        db = tmp_path / "work.db"
        svc = SQLiteWorkService(db_path=db, project_path=tmp_path)

        t = self._make_task(svc)
        reloaded = svc.get(t.task_id)
        assert reloaded.flow_template_version == 1

    def test_yaml_change_bumps_version_for_new_tasks(self, tmp_path):
        """Editing the YAML creates a new version; existing task stays on v1."""
        self._write_custom_standard(tmp_path, description="v1-body")
        db = tmp_path / "work.db"
        svc = SQLiteWorkService(db_path=db, project_path=tmp_path)

        old = self._make_task(svc)
        assert svc.get(old.task_id).flow_template_version == 1

        # Mutate the YAML — description differs, so content hash differs.
        self._write_custom_standard(tmp_path, description="v2-body")

        # Re-open the service so caches (if any) don't mask the test.
        svc.close()
        svc = SQLiteWorkService(db_path=db, project_path=tmp_path)

        new = self._make_task(svc)
        assert svc.get(new.task_id).flow_template_version == 2

        # Old task still pinned to v1
        still_old = svc.get(old.task_id)
        assert still_old.flow_template_version == 1

    def test_in_flight_task_uses_its_pinned_version(self, tmp_path):
        """An in-flight task continues on the v1 graph after YAML is edited."""
        self._write_custom_standard(tmp_path, description="v1-body")
        db = tmp_path / "work.db"
        svc = SQLiteWorkService(db_path=db, project_path=tmp_path)

        old = self._make_task(svc)
        svc.queue(old.task_id, "pm")
        claimed = svc.claim(old.task_id, "pete")
        assert claimed.current_node_id == "implement"

        # Now mutate the YAML. Nothing about old's graph should change.
        self._write_custom_standard(tmp_path, description="v2-body")
        svc.close()
        svc = SQLiteWorkService(db_path=db, project_path=tmp_path)

        # _load_flow_from_db should return the v1 flow for the old task.
        flow = svc._load_flow_from_db(
            claimed.flow_template_id, claimed.flow_template_version,
        )
        assert flow.description == "v1-body"

        # v2 is visible when we resolve via the engine directly (new tasks use it)
        flow_v2 = svc._load_flow_from_db("standard", 2)
        assert flow_v2.description == "v2-body"

    def test_unchanged_yaml_does_not_bump_version(self, tmp_path):
        """Re-opening the service without YAML changes keeps the same version."""
        self._write_custom_standard(tmp_path, description="stable-body")
        db = tmp_path / "work.db"
        svc = SQLiteWorkService(db_path=db, project_path=tmp_path)

        t1 = self._make_task(svc)
        assert svc.get(t1.task_id).flow_template_version == 1

        svc.close()
        svc = SQLiteWorkService(db_path=db, project_path=tmp_path)
        t2 = self._make_task(svc)
        assert svc.get(t2.task_id).flow_template_version == 1


def test_load_flow_from_db_uses_cached_template_on_repeat_load(tmp_path, monkeypatch):
    flows_dir = tmp_path / ".pollypm" / "flows"
    flows_dir.mkdir(parents=True, exist_ok=True)
    (flows_dir / "standard.yaml").write_text(
        """name: standard
description: cached-body
roles:
  worker: worker
  reviewer: reviewer
nodes:
  implement:
    name: Implement
    type: work
    actor_type: role
    actor_role: worker
    next_node: review
  review:
    name: Review
    type: review
    actor_type: role
    actor_role: reviewer
    next_node: done
    reject_node: implement
  done:
    name: Done
    type: terminal
    actor_type: project_manager
start_node: implement
"""
    )
    db = tmp_path / "work.db"
    svc = SQLiteWorkService(db_path=db, project_path=tmp_path)
    task = _create_standard_task(svc)
    flow = svc._load_flow_from_db(
        task.flow_template_id,
        task.flow_template_version,
    )

    class GuardConn:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, params=()):
            if "work_flow_templates" in sql or "work_flow_nodes" in sql:
                raise AssertionError("flow template DB lookup should be served from cache")
            return self._conn.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(svc, "_conn", GuardConn(svc._conn))

    cached = svc._load_flow_from_db(
        task.flow_template_id,
        task.flow_template_version,
    )

    assert cached is flow


def test_load_relationships_uses_one_dependency_query(svc, monkeypatch):
    blocker = _create_standard_task(svc, title="Blocker")
    target = _create_standard_task(svc, title="Target")
    related = _create_standard_task(svc, title="Related")

    svc.link(blocker.task_id, target.task_id, "blocks")
    svc.link(target.task_id, related.task_id, "relates_to")

    dependency_queries = 0

    class GuardConn:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, params=()):
            nonlocal dependency_queries
            if "FROM work_task_dependencies" in sql:
                dependency_queries += 1
            return self._conn.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(svc, "_conn", GuardConn(svc._conn))

    rels = svc._load_relationships(target.project, target.task_number)

    assert dependency_queries == 1
    assert rels["blocked_by"] == [(blocker.project, blocker.task_number)]
    assert rels["relates_to"] == [(related.project, related.task_number)]


# ---------------------------------------------------------------------------
# actor_type=agent — named agent resolution (#140)
# ---------------------------------------------------------------------------


class TestActorTypeAgent:
    """Flow nodes with actor_type=agent must resolve to the specific
    named agent declared in the YAML (issue #140)."""

    def _write_agent_flow(self, root, *, agent_name="polly"):
        """Write a flow that uses actor_type=agent for the work node."""
        import textwrap

        flows_dir = root / ".pollypm" / "flows"
        flows_dir.mkdir(parents=True, exist_ok=True)
        (flows_dir / "pinned.yaml").write_text(textwrap.dedent(f"""\
            name: pinned
            description: Agent-pinned work node
            roles:
              reviewer:
                description: Reviews
            nodes:
              do_it:
                type: work
                actor_type: agent
                agent_name: {agent_name}
                next_node: check
              check:
                type: review
                actor_type: role
                actor_role: reviewer
                next_node: done
                reject_node: do_it
              done:
                type: terminal
            start_node: do_it
        """))

    def test_derive_owner_returns_named_agent(self, tmp_path):
        self._write_agent_flow(tmp_path, agent_name="polly")
        svc = SQLiteWorkService(
            db_path=tmp_path / "w.db", project_path=tmp_path,
        )
        t = svc.create(
            title="Pinned task",
            description="Only polly can do this",
            type="task",
            project="proj",
            flow_template="pinned",
            roles={"reviewer": "rita"},
            priority="normal",
            created_by="tester",
        )
        svc.queue(t.task_id, "pm")
        claimed = svc.claim(t.task_id, "polly")
        owner = svc.derive_owner(claimed)
        assert owner == "polly"

    def test_validate_actor_role_agent_mismatch(self, tmp_path):
        """An actor that is not the pinned agent must fail validation."""
        self._write_agent_flow(tmp_path, agent_name="polly")
        svc = SQLiteWorkService(
            db_path=tmp_path / "w.db", project_path=tmp_path,
        )
        t = svc.create(
            title="Pinned task",
            description="desc",
            type="task",
            project="proj",
            flow_template="pinned",
            roles={"reviewer": "rita"},
            priority="normal",
            created_by="tester",
        )
        svc.queue(t.task_id, "pm")
        svc.claim(t.task_id, "polly")

        # Wrong actor for the pinned agent node must surface as a hard
        # actor_role failure from validate_advance.
        results = svc.validate_advance(t.task_id, "impostor")
        actor_failures = [
            r for r in results
            if r.gate_name == "actor_role" and not r.passed
        ]
        assert len(actor_failures) == 1
        assert "polly" in actor_failures[0].reason

    def test_flow_validation_rejects_agent_without_name(self, tmp_path):
        """YAML with actor_type=agent but no agent_name must fail validation."""
        import textwrap
        from pollypm.work.flow_engine import FlowValidationError, parse_flow_yaml

        yaml_text = textwrap.dedent("""\
            name: broken
            description: missing agent_name
            roles: {}
            nodes:
              do_it:
                type: work
                actor_type: agent
                next_node: done
              done:
                type: terminal
            start_node: do_it
        """)
        with pytest.raises(FlowValidationError, match="agent_name"):
            parse_flow_yaml(yaml_text)
