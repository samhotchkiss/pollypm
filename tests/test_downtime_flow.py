"""Tests for dt02 — downtime_explore flow + human approval + validator.

Covers:

* Bundled ``downtime_explore`` flow resolves through the flow engine
  and parses cleanly.
* Never-auto-deploy validator:
    - accepts the bundled flow.
    - rejects a flow missing the human approval node.
    - rejects a flow whose approval next_node is terminal (short-circuit).
    - rejects a flow whose approval node lacks ``inbox_notification_sent``.
    - rejects a flow with multiple human nodes.
* ``inbox_notification_sent`` gate:
    - fails on a task with no marker.
    - passes once a marker is logged to the task context.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from pollypm.plugins_builtin.downtime.flow_validator import (
    DowntimeFlowValidationError,
    assert_downtime_flow_shape,
    is_downtime_flow,
)
from pollypm.plugins_builtin.downtime.gates.inbox_notification_sent import (
    InboxNotificationSent,
)
from pollypm.work.flow_engine import parse_flow_yaml, resolve_flow
from pollypm.work.models import (
    ActorType,
    ContextEntry,
    FlowNode,
    FlowTemplate,
    NodeType,
    Task,
    TaskType,
    WorkStatus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task_with_context(texts: list[str]) -> Task:
    now = datetime.now(timezone.utc)
    task = Task(
        project="fixture",
        task_number=1,
        title="t",
        type=TaskType.TASK,
        work_status=WorkStatus.IN_PROGRESS,
    )
    task.context = [ContextEntry(actor="downtime", timestamp=now, text=text) for text in texts]
    return task


def _valid_flow_yaml() -> str:
    return """
name: downtime_explore_test
description: test downtime explore flow
roles:
  explorer:
    description: explorer
nodes:
  explore:
    type: work
    actor_type: role
    actor_role: explorer
    next_node: awaiting_approval
  awaiting_approval:
    type: review
    actor_type: human
    gates: [inbox_notification_sent]
    next_node: apply
    reject_node: apply
  apply:
    type: work
    actor_type: role
    actor_role: explorer
    next_node: done
  done:
    type: terminal
start_node: explore
""".strip()


# ---------------------------------------------------------------------------
# Bundled flow resolves
# ---------------------------------------------------------------------------


class TestBundledFlow:
    def test_flow_resolves(self) -> None:
        flow = resolve_flow("downtime_explore")
        assert flow.name == "downtime_explore"
        assert set(flow.nodes.keys()) == {"explore", "awaiting_approval", "apply", "done"}
        assert flow.start_node == "explore"

    def test_bundled_flow_passes_validator(self) -> None:
        flow = resolve_flow("downtime_explore")
        # Should not raise.
        assert_downtime_flow_shape(flow)

    def test_is_downtime_flow_heuristic(self) -> None:
        flow = resolve_flow("downtime_explore")
        assert is_downtime_flow(flow) is True

    def test_budget_on_explore_node(self) -> None:
        flow = resolve_flow("downtime_explore")
        assert flow.nodes["explore"].budget_seconds == 1800


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class TestFlowValidator:
    def test_valid_flow_accepted(self) -> None:
        flow = parse_flow_yaml(_valid_flow_yaml())
        assert_downtime_flow_shape(flow)  # does not raise

    def test_missing_human_node_rejected(self) -> None:
        bad = _valid_flow_yaml().replace("actor_type: human", "actor_type: role\n    actor_role: explorer")
        flow = parse_flow_yaml(bad)
        with pytest.raises(DowntimeFlowValidationError) as exc_info:
            assert_downtime_flow_shape(flow)
        assert "human-actor node" in str(exc_info.value)

    def test_approval_short_circuits_to_terminal_rejected(self) -> None:
        # Build the template directly — the generic flow-engine parser
        # rejects orphan nodes, so we can't express "approval → done
        # (bypassing apply)" via the YAML path. The downtime validator
        # is still the right layer to catch it because it runs on
        # *any* FlowTemplate (including ones parsed from alternative
        # sources).
        flow = FlowTemplate(
            name="downtime_short_circuit",
            description="approval routes straight to terminal",
            roles={"explorer": {"description": "e"}},
            nodes={
                "explore": FlowNode(
                    name="explore",
                    type=NodeType.WORK,
                    actor_type=ActorType.ROLE,
                    actor_role="explorer",
                    next_node_id="awaiting_approval",
                ),
                "awaiting_approval": FlowNode(
                    name="awaiting_approval",
                    type=NodeType.REVIEW,
                    actor_type=ActorType.HUMAN,
                    next_node_id="done",
                    reject_node_id="done",
                    gates=["inbox_notification_sent"],
                ),
                "done": FlowNode(name="done", type=NodeType.TERMINAL),
            },
            start_node="explore",
        )
        with pytest.raises(DowntimeFlowValidationError) as exc_info:
            assert_downtime_flow_shape(flow)
        assert "terminal" in str(exc_info.value)

    def test_missing_required_gate_rejected(self) -> None:
        bad = _valid_flow_yaml().replace("[inbox_notification_sent]", "[]")
        flow = parse_flow_yaml(bad)
        with pytest.raises(DowntimeFlowValidationError) as exc_info:
            assert_downtime_flow_shape(flow)
        assert "inbox_notification_sent" in str(exc_info.value)

    def test_two_human_nodes_rejected(self) -> None:
        bad_yaml = """
name: downtime_bad_two_humans
description: two humans
roles:
  explorer:
    description: e
nodes:
  explore:
    type: work
    actor_type: role
    actor_role: explorer
    next_node: first_human
  first_human:
    type: review
    actor_type: human
    gates: [inbox_notification_sent]
    next_node: second_human
    reject_node: second_human
  second_human:
    type: review
    actor_type: human
    gates: [inbox_notification_sent]
    next_node: apply
    reject_node: apply
  apply:
    type: work
    actor_type: role
    actor_role: explorer
    next_node: done
  done:
    type: terminal
start_node: explore
""".strip()
        flow = parse_flow_yaml(bad_yaml)
        with pytest.raises(DowntimeFlowValidationError) as exc_info:
            assert_downtime_flow_shape(flow)
        assert "found 2" in str(exc_info.value)

    def test_bypass_path_rejected(self) -> None:
        """A flow where an alternate edge reaches terminal without the human."""
        bad_yaml = """
name: downtime_bypass
description: bypass exists
roles:
  explorer:
    description: e
nodes:
  explore:
    type: work
    actor_type: role
    actor_role: explorer
    next_node: bypass_review
  bypass_review:
    type: review
    actor_type: role
    actor_role: explorer
    next_node: awaiting_approval
    reject_node: done
  awaiting_approval:
    type: review
    actor_type: human
    gates: [inbox_notification_sent]
    next_node: apply
    reject_node: apply
  apply:
    type: work
    actor_type: role
    actor_role: explorer
    next_node: done
  done:
    type: terminal
start_node: explore
""".strip()
        flow = parse_flow_yaml(bad_yaml)
        with pytest.raises(DowntimeFlowValidationError) as exc_info:
            assert_downtime_flow_shape(flow)
        assert "bypass" in str(exc_info.value).lower() or "bypasses" in str(exc_info.value)


# ---------------------------------------------------------------------------
# inbox_notification_sent gate
# ---------------------------------------------------------------------------


class TestInboxNotificationSentGate:
    def test_name_and_type(self) -> None:
        gate = InboxNotificationSent()
        assert gate.name == "inbox_notification_sent"
        assert gate.gate_type == "hard"

    def test_fails_with_no_context(self) -> None:
        gate = InboxNotificationSent()
        task = _task_with_context([])
        result = gate.check(task)
        assert result.passed is False
        assert "not been dispatched" in result.reason

    def test_fails_with_unrelated_context(self) -> None:
        gate = InboxNotificationSent()
        task = _task_with_context(["initial context", "explorer started work"])
        result = gate.check(task)
        assert result.passed is False

    def test_passes_with_marker(self) -> None:
        gate = InboxNotificationSent()
        task = _task_with_context(["inbox_notification_sent: entry 42 for fixture/1"])
        result = gate.check(task)
        assert result.passed is True

    def test_passes_with_tagged_marker(self) -> None:
        gate = InboxNotificationSent()
        task = _task_with_context(
            ["[downtime] inbox_notification_sent: entry 42 for fixture/1"]
        )
        result = gate.check(task)
        assert result.passed is True


# ---------------------------------------------------------------------------
# Initialize hook surfaces validator verdict
# ---------------------------------------------------------------------------


class TestInitializeHookValidates:
    def test_bundled_flow_emits_ok(self) -> None:
        from pollypm.plugin_api.v1 import JobHandlerAPI, PluginAPI, RosterAPI
        from pollypm.heartbeat.roster import Roster
        from pollypm.jobs import JobHandlerRegistry
        from pollypm.plugins_builtin.downtime import plugin as plugin_module

        events: list[tuple[str, dict]] = []

        class _StubStore:
            def record_event(self, *, kind: str, payload: dict) -> None:
                events.append((kind, payload))

        api = PluginAPI(
            plugin_name="downtime",
            roster_api=RosterAPI(Roster(), plugin_name="downtime"),
            jobs_api=JobHandlerAPI(JobHandlerRegistry(), plugin_name="downtime"),
            state_store=_StubStore(),
        )
        plugin_module.initialize(api)
        init_events = [p for k, p in events if k.endswith(".initialize")]
        assert init_events, "initialize event not emitted"
        payload = init_events[-1]
        assert payload["flow_validator_ok"] is True
        assert payload["flow_validator_detail"] == "ok"
