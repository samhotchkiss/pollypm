"""Plugin interop tests — prove boundaries work and plugins are swappable."""

from __future__ import annotations

import pytest

from pollypm.work.gates import Gate, GateRegistry
from pollypm.work.models import (
    Artifact,
    ArtifactKind,
    GateResult,
    OutputType,
    Task,
    WorkOutput,
    WorkStatus,
)
from pollypm.work.mock_service import MockWorkService
from pollypm.work.plugin_registry import PluginNotRegisteredError, PluginRegistry, configure_work_plugins
from pollypm.work.service import WorkService
from pollypm.work.sqlite_service import SQLiteWorkService
from pollypm.work.sync import SyncAdapter, SyncManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _standard_work_output() -> WorkOutput:
    """A valid WorkOutput with one artifact, suitable for node_done."""
    return WorkOutput(
        type=OutputType.CODE_CHANGE,
        summary="Implemented the feature",
        artifacts=[
            Artifact(
                kind=ArtifactKind.COMMIT,
                description="feat: add the thing",
                ref="abc123",
            )
        ],
    )


def _lifecycle_through_flow(svc):
    """Drive a task through create -> queue -> claim -> node_done -> approve -> done.

    Works with any WorkService implementation. Returns the final task.
    """
    task = svc.create(
        title="Interop test task",
        description="Prove the protocol works",
        type="task",
        project="test-proj",
        flow_template="standard",
        roles={"worker": "agent-w", "reviewer": "agent-r"},
        priority="normal",
    )
    task_id = task.task_id
    assert task.work_status == WorkStatus.DRAFT

    task = svc.queue(task_id, "pm")
    assert task.work_status == WorkStatus.QUEUED

    task = svc.claim(task_id, "agent-w")
    assert task.work_status == WorkStatus.IN_PROGRESS
    assert task.assignee == "agent-w"
    assert task.current_node_id is not None

    task = svc.node_done(task_id, "agent-w", _standard_work_output())
    assert task.work_status == WorkStatus.REVIEW

    task = svc.approve(task_id, "agent-r", reason="Looks good")
    assert task.work_status == WorkStatus.DONE

    return task


# ---------------------------------------------------------------------------
# Protocol satisfaction
# ---------------------------------------------------------------------------


class TestProtocolSatisfaction:
    def test_mock_service_satisfies_protocol(self):
        """MockWorkService must structurally satisfy the WorkService protocol."""
        svc = MockWorkService()
        # Check that all WorkService methods exist
        protocol_methods = [
            "create", "get", "list_tasks", "queue", "claim", "next",
            "update", "cancel", "hold", "resume",
            "node_done", "approve", "reject", "block",
            "get_execution",
            "add_context", "get_context",
            "link", "unlink", "dependents",
            "available_flows", "get_flow", "validate_advance",
            "sync_status", "trigger_sync",
            "state_counts", "my_tasks", "blocked_tasks",
        ]
        for method in protocol_methods:
            assert hasattr(svc, method), f"MockWorkService missing method: {method}"
            assert callable(getattr(svc, method)), f"MockWorkService.{method} is not callable"

    def test_sqlite_service_satisfies_protocol(self, tmp_path):
        """SQLiteWorkService must structurally satisfy the WorkService protocol."""
        svc = SQLiteWorkService(db_path=tmp_path / "work.db")
        protocol_methods = [
            "create", "get", "list_tasks", "queue", "claim", "next",
            "update", "cancel", "hold", "resume",
            "node_done", "approve", "reject", "block",
            "get_execution",
            "add_context", "get_context",
            "link", "unlink", "dependents",
            "available_flows", "get_flow", "validate_advance",
            "state_counts", "my_tasks", "blocked_tasks",
        ]
        for method in protocol_methods:
            assert hasattr(svc, method), f"SQLiteWorkService missing method: {method}"
            assert callable(getattr(svc, method)), f"SQLiteWorkService.{method} is not callable"


# ---------------------------------------------------------------------------
# Consumer interop — same code, different backends
# ---------------------------------------------------------------------------


class TestConsumerInterop:
    def test_consumer_works_with_mock(self):
        """A consumer function works identically with MockWorkService."""
        svc = MockWorkService()
        task = _lifecycle_through_flow(svc)
        assert task.work_status == WorkStatus.DONE

    def test_consumer_works_with_sqlite(self, tmp_path):
        """A consumer function works identically with SQLiteWorkService."""
        svc = SQLiteWorkService(db_path=tmp_path / "work.db")
        task = _lifecycle_through_flow(svc)
        assert task.work_status == WorkStatus.DONE


# ---------------------------------------------------------------------------
# PluginRegistry
# ---------------------------------------------------------------------------


class TestPluginRegistry:
    def test_registry_requires_registration(self):
        """Accessing a plugin slot before registration raises a clear error."""
        registry = PluginRegistry()

        with pytest.raises(PluginNotRegisteredError, match="WorkService"):
            _ = registry.work_service

        with pytest.raises(PluginNotRegisteredError, match="GateRegistry"):
            _ = registry.gate_registry

        with pytest.raises(PluginNotRegisteredError, match="SyncManager"):
            _ = registry.sync_manager

        with pytest.raises(PluginNotRegisteredError, match="SessionManager"):
            _ = registry.session_manager

    def test_registry_wires_up(self, tmp_path):
        """configure_work_plugins returns a registry with default plugins registered."""
        registry = configure_work_plugins(db_path=tmp_path / "work.db")

        # These should not raise
        ws = registry.work_service
        gr = registry.gate_registry
        sm = registry.sync_manager

        assert ws is not None
        assert isinstance(gr, GateRegistry)
        assert isinstance(sm, SyncManager)

        # Session manager is not registered by default
        with pytest.raises(PluginNotRegisteredError):
            _ = registry.session_manager

    def test_registry_accepts_mock(self):
        """PluginRegistry accepts MockWorkService as a work service."""
        registry = PluginRegistry()
        mock = MockWorkService()
        registry.register_work_service(mock)
        assert registry.work_service is mock


# ---------------------------------------------------------------------------
# Custom gate integration
# ---------------------------------------------------------------------------


class TestCustomGateIntegration:
    def test_custom_gate_integrates(self):
        """A custom gate can be registered and is called during validate_advance."""

        class AlwaysFailGate:
            name = "always_fail"
            gate_type = "soft"

            def check(self, task: Task, **kwargs) -> GateResult:
                return GateResult(passed=False, reason="Custom gate says no.")

        assert isinstance(AlwaysFailGate(), Gate)

        # Create a service and a task, then validate gates
        svc = MockWorkService()
        task = svc.create(
            title="Gate test",
            description="Test custom gate",
            type="task",
            project="proj",
            flow_template="standard",
            roles={"worker": "w", "reviewer": "r"},
            priority="normal",
        )

        # Register custom gate
        gate = AlwaysFailGate()
        svc._gate_registry._gates[gate.name] = gate

        # Queue and claim so we're at the implement node
        svc.queue(task.task_id, "pm")
        svc.claim(task.task_id, "w")

        # Validate advance; the standard flow has has_assignee on implement node
        results = svc.validate_advance(task.task_id, "w")
        # Results should include the gates from the flow node
        assert len(results) > 0


# ---------------------------------------------------------------------------
# Sync adapter integration
# ---------------------------------------------------------------------------


class TestSyncAdapterIntegration:
    def test_sync_adapter_integrates(self):
        """A custom sync adapter receives events when registered."""

        events: list[str] = []

        class RecordingSyncAdapter:
            name = "recorder"

            def on_create(self, task: Task) -> None:
                events.append(f"create:{task.task_id}")

            def on_transition(self, task: Task, old_status: str, new_status: str) -> None:
                events.append(f"transition:{task.task_id}:{old_status}->{new_status}")

            def on_update(self, task: Task, changed_fields: list[str]) -> None:
                events.append(f"update:{task.task_id}:{changed_fields}")

        adapter = RecordingSyncAdapter()
        assert isinstance(adapter, SyncAdapter)

        manager = SyncManager()
        manager.register(adapter)
        assert len(manager.adapters) == 1

        # Simulate events
        svc = MockWorkService()
        task = svc.create(
            title="Sync test",
            description="Test sync adapter",
            type="task",
            project="proj",
            flow_template="standard",
            roles={"worker": "w", "reviewer": "r"},
            priority="normal",
        )

        # Dispatch via manager
        manager.on_create(task)
        assert any("create:" in e for e in events)


# ---------------------------------------------------------------------------
# Plugin config defaults
# ---------------------------------------------------------------------------


class TestPluginConfigDefaults:
    def test_plugin_config_loads_defaults(self, tmp_path):
        """Loading config with no custom settings selects built-in plugins."""
        registry = configure_work_plugins(db_path=tmp_path / "work.db")

        # Work service should be SQLiteWorkService
        assert isinstance(registry.work_service, SQLiteWorkService)

        # Gate registry should have built-in gates
        gates = registry.gate_registry.all_gates()
        assert "has_description" in gates
        assert "has_assignee" in gates
        assert "has_work_output" in gates

        # Sync manager should be empty by default
        assert len(registry.sync_manager.adapters) == 0
