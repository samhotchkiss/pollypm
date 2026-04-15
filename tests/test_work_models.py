"""Tests for work service data models."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pollypm.work.models import (
    Artifact,
    ArtifactKind,
    ActorType,
    ContextEntry,
    Decision,
    ExecutionStatus,
    FlowNode,
    FlowNodeExecution,
    FlowTemplate,
    GateResult,
    LinkKind,
    NodeType,
    OutputType,
    Priority,
    Task,
    TaskType,
    Transition,
    WorkOutput,
    WorkStatus,
)


# ---------------------------------------------------------------------------
# Enum tests
# ---------------------------------------------------------------------------


class TestWorkStatusEnum:
    def test_all_eight_states_exist(self):
        values = {s.value for s in WorkStatus}
        assert values == {
            "draft",
            "queued",
            "in_progress",
            "blocked",
            "on_hold",
            "review",
            "done",
            "cancelled",
        }

    def test_lookup_by_value(self):
        assert WorkStatus("draft") is WorkStatus.DRAFT
        assert WorkStatus("in_progress") is WorkStatus.IN_PROGRESS

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            WorkStatus("nonexistent")


class TestOtherEnums:
    def test_task_type_values(self):
        assert {t.value for t in TaskType} == {
            "epic", "task", "subtask", "bug", "spike",
        }

    def test_priority_values(self):
        assert {p.value for p in Priority} == {
            "critical", "high", "normal", "low",
        }

    def test_node_type_values(self):
        assert {n.value for n in NodeType} == {"work", "review", "terminal"}

    def test_actor_type_values(self):
        assert {a.value for a in ActorType} == {
            "role", "agent", "human", "project_manager",
        }

    def test_execution_status_values(self):
        assert {e.value for e in ExecutionStatus} == {
            "pending", "active", "blocked", "completed",
        }

    def test_decision_values(self):
        assert {d.value for d in Decision} == {"approved", "rejected"}

    def test_output_type_values(self):
        assert {o.value for o in OutputType} == {
            "code_change", "action", "document", "mixed",
        }

    def test_artifact_kind_values(self):
        assert {a.value for a in ArtifactKind} == {
            "commit", "file_change", "action", "note",
        }

    def test_link_kind_values(self):
        assert {k.value for k in LinkKind} == {
            "blocks", "relates_to", "supersedes", "parent",
        }


# ---------------------------------------------------------------------------
# Dataclass construction
# ---------------------------------------------------------------------------


class TestTask:
    def _make_task(self, **overrides):
        defaults = dict(
            project="myproject",
            task_number=1,
            title="Fix the widget",
            type=TaskType.TASK,
            work_status=WorkStatus.DRAFT,
            flow_template_id="standard",
            priority=Priority.NORMAL,
            description="Some work to do",
            created_at=datetime(2026, 4, 14, tzinfo=timezone.utc),
            created_by="polly",
            updated_at=datetime(2026, 4, 14, tzinfo=timezone.utc),
        )
        defaults.update(overrides)
        return Task(**defaults)

    def test_basic_construction(self):
        t = self._make_task()
        assert t.project == "myproject"
        assert t.task_number == 1
        assert t.title == "Fix the widget"
        assert t.type is TaskType.TASK
        assert t.work_status is WorkStatus.DRAFT

    def test_task_id_property(self):
        t = self._make_task(project="acme", task_number=42)
        assert t.task_id == "acme/42"

    def test_owner_none_when_no_current_node(self):
        t = self._make_task(current_node_id=None)
        assert t.owner is None

    def test_owner_returns_assignee_when_on_node(self):
        t = self._make_task(current_node_id="implement", assignee="pete")
        assert t.owner == "pete"

    def test_blocked_derived_from_status(self):
        t = self._make_task(work_status=WorkStatus.BLOCKED)
        assert t.blocked is True

    def test_not_blocked_when_in_progress(self):
        t = self._make_task(work_status=WorkStatus.IN_PROGRESS)
        assert t.blocked is False

    def test_defaults(self):
        t = self._make_task()
        assert t.labels == []
        assert t.relevant_files == []
        assert t.roles == {}
        assert t.external_refs == {}
        assert t.context == []
        assert t.transitions == []
        assert t.executions == []
        assert t.requires_human_review is False

    def test_json_helpers(self):
        t = self._make_task(
            labels=["ui", "bug"],
            roles={"worker": "pete"},
            external_refs={"github": "org/repo#1"},
            relevant_files=["src/foo.py"],
        )
        assert '"ui"' in t.labels_json()
        assert '"worker"' in t.roles_json()
        assert '"github"' in t.external_refs_json()
        assert '"src/foo.py"' in t.relevant_files_json()


class TestFlowTemplate:
    def test_construction(self):
        node = FlowNode(
            name="implement",
            type=NodeType.WORK,
            actor_type=ActorType.ROLE,
            actor_role="worker",
            next_node_id="review",
        )
        ft = FlowTemplate(
            name="standard",
            description="Default flow",
            roles={"worker": {"description": "Does the work"}},
            nodes={"implement": node},
            start_node="implement",
            version=1,
            is_current=True,
        )
        assert ft.name == "standard"
        assert ft.nodes["implement"].type is NodeType.WORK


class TestFlowNodeExecution:
    def test_construction(self):
        ex = FlowNodeExecution(
            task_id="proj/1",
            node_id="implement",
            visit=1,
            status=ExecutionStatus.ACTIVE,
            started_at=datetime(2026, 4, 14, tzinfo=timezone.utc),
        )
        assert ex.visit == 1
        assert ex.status is ExecutionStatus.ACTIVE
        assert ex.work_output is None
        assert ex.decision is None


class TestWorkOutput:
    def test_construction(self):
        a = Artifact(
            kind=ArtifactKind.COMMIT,
            ref="abc123",
            description="Fixed cookie attribute",
        )
        wo = WorkOutput(
            type=OutputType.CODE_CHANGE,
            summary="Fixed SameSite cookie",
            artifacts=[a],
        )
        assert wo.type is OutputType.CODE_CHANGE
        assert len(wo.artifacts) == 1
        assert wo.artifacts[0].ref == "abc123"


class TestContextEntry:
    def test_construction(self):
        ce = ContextEntry(
            actor="polly",
            timestamp=datetime(2026, 4, 14, tzinfo=timezone.utc),
            text="Initial scoping complete",
        )
        assert ce.actor == "polly"


class TestGateResult:
    def test_pass(self):
        gr = GateResult(passed=True, reason="All good")
        assert gr.passed is True

    def test_fail(self):
        gr = GateResult(passed=False, reason="Missing description")
        assert gr.passed is False


class TestTransition:
    def test_construction(self):
        tr = Transition(
            from_state="draft",
            to_state="queued",
            actor="polly",
            timestamp=datetime(2026, 4, 14, tzinfo=timezone.utc),
            reason=None,
        )
        assert tr.from_state == "draft"
        assert tr.to_state == "queued"
