"""Tests for the gate protocol, built-in gates, registry, and evaluation."""

from __future__ import annotations

import textwrap

import pytest

from pollypm.work.gates import (
    AcceptanceCriteria,
    AllChildrenDone,
    GateRegistry,
    HasAssignee,
    HasCommits,
    HasDescription,
    HasWorkOutput,
    evaluate_gates,
    has_hard_failure,
)
from pollypm.work.models import (
    Artifact,
    ArtifactKind,
    ExecutionStatus,
    FlowNodeExecution,
    OutputType,
    Task,
    TaskType,
    WorkOutput,
    WorkStatus,
)
from pollypm.work.sqlite_service import (
    SQLiteWorkService,
    ValidationError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(**overrides) -> Task:
    """Create a minimal Task for unit-testing gates."""
    defaults = dict(
        project="proj",
        task_number=1,
        title="Test task",
        type=TaskType.TASK,
    )
    defaults.update(overrides)
    return Task(**defaults)


def _make_execution(
    node_id: str = "implement",
    artifacts: list[Artifact] | None = None,
    status: ExecutionStatus = ExecutionStatus.ACTIVE,
) -> FlowNodeExecution:
    """Create a FlowNodeExecution with optional work output."""
    wo = None
    if artifacts is not None:
        wo = WorkOutput(
            type=OutputType.CODE_CHANGE,
            summary="work done",
            artifacts=artifacts,
        )
    return FlowNodeExecution(
        task_id="proj/1",
        node_id=node_id,
        visit=1,
        status=status,
        work_output=wo,
    )


# ---------------------------------------------------------------------------
# has_description
# ---------------------------------------------------------------------------


class TestHasDescription:
    def test_passes(self):
        task = _make_task(description="Build the widget")
        result = HasDescription().check(task)
        assert result.passed is True

    def test_fails_empty(self):
        task = _make_task(description="")
        result = HasDescription().check(task)
        assert result.passed is False
        assert "no description" in result.reason.lower()

    def test_fails_whitespace(self):
        task = _make_task(description="   ")
        result = HasDescription().check(task)
        assert result.passed is False


# ---------------------------------------------------------------------------
# has_assignee
# ---------------------------------------------------------------------------


class TestHasAssignee:
    def test_passes(self):
        task = _make_task(assignee="agent-1")
        result = HasAssignee().check(task)
        assert result.passed is True

    def test_fails_none(self):
        task = _make_task(assignee=None)
        result = HasAssignee().check(task)
        assert result.passed is False
        assert "no assignee" in result.reason.lower()

    def test_fails_empty(self):
        task = _make_task(assignee="")
        result = HasAssignee().check(task)
        assert result.passed is False


# ---------------------------------------------------------------------------
# has_work_output
# ---------------------------------------------------------------------------


class TestHasWorkOutput:
    def test_passes(self):
        art = Artifact(kind=ArtifactKind.FILE_CHANGE, description="changed foo.py")
        exe = _make_execution(artifacts=[art])
        task = _make_task(
            current_node_id="implement",
            executions=[exe],
        )
        result = HasWorkOutput().check(task)
        assert result.passed is True

    def test_fails_no_output(self):
        exe = _make_execution(artifacts=None)
        task = _make_task(
            current_node_id="implement",
            executions=[exe],
        )
        result = HasWorkOutput().check(task)
        assert result.passed is False

    def test_fails_empty_artifacts(self):
        exe = _make_execution(artifacts=[])
        task = _make_task(
            current_node_id="implement",
            executions=[exe],
        )
        result = HasWorkOutput().check(task)
        assert result.passed is False


# ---------------------------------------------------------------------------
# acceptance_criteria
# ---------------------------------------------------------------------------


class TestAcceptanceCriteria:
    def test_passes(self):
        task = _make_task(acceptance_criteria="Widget renders correctly")
        gate = AcceptanceCriteria()
        assert gate.gate_type == "soft"
        result = gate.check(task)
        assert result.passed is True

    def test_fails(self):
        task = _make_task(acceptance_criteria=None)
        result = AcceptanceCriteria().check(task)
        assert result.passed is False
        assert "no acceptance criteria" in result.reason.lower()


# ---------------------------------------------------------------------------
# has_commits
# ---------------------------------------------------------------------------


class TestHasCommits:
    def test_passes(self):
        art = Artifact(kind=ArtifactKind.COMMIT, description="feat: add widget", ref="abc123")
        exe = _make_execution(artifacts=[art])
        task = _make_task(executions=[exe])
        gate = HasCommits()
        assert gate.gate_type == "soft"
        result = gate.check(task)
        assert result.passed is True

    def test_fails_no_commits(self):
        art = Artifact(kind=ArtifactKind.FILE_CHANGE, description="changed foo.py")
        exe = _make_execution(artifacts=[art])
        task = _make_task(executions=[exe])
        result = HasCommits().check(task)
        assert result.passed is False


# ---------------------------------------------------------------------------
# all_children_done
# ---------------------------------------------------------------------------


class TestAllChildrenDone:
    def test_passes_no_children(self):
        task = _make_task(children=[])
        result = AllChildrenDone().check(task)
        assert result.passed is True

    def test_passes_all_done(self):
        child_done = _make_task(
            task_number=2,
            work_status=WorkStatus.DONE,
        )
        parent = _make_task(children=[("proj", 2)])

        def get_task(tid: str) -> Task:
            return child_done

        result = AllChildrenDone().check(parent, get_task=get_task)
        assert result.passed is True

    def test_fails_child_in_progress(self):
        child_ip = _make_task(
            task_number=2,
            work_status=WorkStatus.IN_PROGRESS,
        )
        parent = _make_task(children=[("proj", 2)])

        def get_task(tid: str) -> Task:
            return child_ip

        result = AllChildrenDone().check(parent, get_task=get_task)
        assert result.passed is False
        assert "in_progress" in result.reason


# ---------------------------------------------------------------------------
# evaluate_gates
# ---------------------------------------------------------------------------


class TestEvaluateGates:
    @pytest.fixture
    def registry(self, tmp_path):
        return GateRegistry(
            project_path=tmp_path,
            user_gates_dir=tmp_path / "no_user_gates",
        )

    def test_all_pass(self, registry):
        task = _make_task(description="ok", assignee="agent-1")
        results = evaluate_gates(task, ["has_description", "has_assignee"], registry)
        assert all(r.passed for r in results)
        assert len(results) == 2

    def test_hard_failure(self, registry):
        task = _make_task(description="")
        results = evaluate_gates(task, ["has_description"], registry)
        assert len(results) == 1
        assert results[0].passed is False
        assert results[0].gate_type == "hard"
        assert has_hard_failure(results)

    def test_soft_failure(self, registry):
        task = _make_task(acceptance_criteria=None)
        results = evaluate_gates(task, ["acceptance_criteria"], registry)
        assert len(results) == 1
        assert results[0].passed is False
        assert results[0].gate_type == "soft"
        assert not has_hard_failure(results)

    def test_unknown_gate(self, registry):
        task = _make_task()
        results = evaluate_gates(task, ["nonexistent_gate"], registry)
        assert len(results) == 1
        assert results[0].passed is False
        assert "Unknown gate" in results[0].reason


# ---------------------------------------------------------------------------
# GateRegistry
# ---------------------------------------------------------------------------


class TestGateRegistry:
    def test_builtin_gates(self, tmp_path):
        registry = GateRegistry(
            project_path=tmp_path,
            user_gates_dir=tmp_path / "no_user_gates",
        )
        gates = registry.all_gates()
        expected = {
            "has_description",
            "has_assignee",
            "has_work_output",
            "has_commits",
            "acceptance_criteria",
            "all_children_done",
        }
        assert expected == set(gates.keys())

    def test_custom_discovery(self, tmp_path):
        gates_dir = tmp_path / ".pollypm" / "gates"
        gates_dir.mkdir(parents=True)
        custom_gate = gates_dir / "custom_check.py"
        custom_gate.write_text(textwrap.dedent("""\
            from pollypm.work.models import Task, GateResult

            class CustomCheck:
                name = "custom_check"
                gate_type = "soft"

                def check(self, task: Task, **kwargs) -> GateResult:
                    return GateResult(passed=True, reason="Custom OK.")
        """))

        registry = GateRegistry(
            project_path=tmp_path,
            user_gates_dir=tmp_path / "no_user_gates",
        )
        gate = registry.get("custom_check")
        assert gate is not None
        assert gate.name == "custom_check"
        assert gate.gate_type == "soft"

    def test_project_overrides_builtin(self, tmp_path):
        """A project-local gate with the same name overrides the built-in."""
        gates_dir = tmp_path / ".pollypm" / "gates"
        gates_dir.mkdir(parents=True)
        (gates_dir / "override.py").write_text(textwrap.dedent("""\
            from pollypm.work.models import Task, GateResult

            class HasDescription:
                name = "has_description"
                gate_type = "soft"

                def check(self, task: Task, **kwargs) -> GateResult:
                    return GateResult(passed=True, reason="Always passes (overridden).")
        """))

        registry = GateRegistry(
            project_path=tmp_path,
            user_gates_dir=tmp_path / "no_user_gates",
        )
        gate = registry.get("has_description")
        assert gate is not None
        assert gate.gate_type == "soft"  # overridden from hard to soft


# ---------------------------------------------------------------------------
# Integration with SQLiteWorkService
# ---------------------------------------------------------------------------


@pytest.fixture
def svc(tmp_path):
    db_path = tmp_path / "work.db"
    return SQLiteWorkService(db_path=db_path)


def _create_standard_task(svc, project="proj", title="My task", description="Do the thing", **kwargs):
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


class TestSkipGates:
    def test_skip_gates_allows_queue_without_description(self, svc):
        """queue a task with no description but skip_gates=True succeeds."""
        task = _create_standard_task(svc, description="")
        # Without skip_gates, should fail
        with pytest.raises(ValidationError, match="gate failed"):
            svc.queue(task.task_id, "pm")

        # With skip_gates, should succeed with warning logged
        queued = svc.queue(task.task_id, "pm", skip_gates=True)
        assert queued.work_status == WorkStatus.QUEUED
        # Check that the transition has a reason mentioning skip-gates
        assert any(
            t.reason and "skip-gates" in t.reason
            for t in queued.transitions
        )


class TestValidateAdvance:
    def test_dry_run(self, svc):
        """Create and claim a task. validate_advance shows gate results."""
        task = _create_standard_task(svc)
        svc.queue(task.task_id, "pm")
        claimed = svc.claim(task.task_id, "agent-1")
        assert claimed.work_status == WorkStatus.IN_PROGRESS

        # The standard flow's implement node has gates: [has_assignee]
        results = svc.validate_advance(claimed.task_id, "agent-1")
        assert len(results) >= 1
        # has_assignee should pass since claim set assignee to agent-1
        assignee_results = [r for r in results if r.gate_name == "has_assignee"]
        assert len(assignee_results) == 1
        assert assignee_results[0].passed is True

    def test_dry_run_no_node(self, svc):
        """Draft task with no current node returns empty list."""
        task = _create_standard_task(svc)
        results = svc.validate_advance(task.task_id, "pm")
        assert results == []
