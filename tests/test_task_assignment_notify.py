"""Tests for the task-assignment notification subsystem — #244.

Covers:

* role-resolution naming conventions (worker/reviewer/operator/critic/agent);
* dedupe throttle (30-min window);
* escalation to alerts when no session matches;
* sweeper behaviour over queued/review tasks;
* end-to-end integration: a task transition to queued with
  ``actor_type=role`` triggers a ``SessionService.send()`` to the
  expected session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from pollypm.plugins_builtin.task_assignment_notify.handlers.notify import (
    event_to_payload,
)
from pollypm.plugins_builtin.task_assignment_notify.resolver import (
    _RuntimeServices,
    DEDUPE_WINDOW_SECONDS,
    notify,
)
from pollypm.plugins_builtin.task_assignment_notify.handlers.sweep import (
    task_assignment_sweep_handler,
)
from pollypm.storage.state import StateStore
from pollypm.work.models import ActorType
from pollypm.work.sqlite_service import SQLiteWorkService
from pollypm.work import task_assignment as bus
from pollypm.work.task_assignment import (
    SessionRoleIndex,
    TaskAssignmentEvent,
    build_event_from_task,
    format_ping_for_role,
    role_candidate_names,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class FakeHandle:
    name: str


@dataclass
class FakeSessionService:
    handles: list[FakeHandle]
    sent: list[tuple[str, str]] = field(default_factory=list)
    send_failure: Exception | None = None

    def list(self) -> list[FakeHandle]:
        return list(self.handles)

    def send(self, name: str, text: str, *, press_enter: bool = True) -> None:
        if self.send_failure is not None:
            raise self.send_failure
        self.sent.append((name, text))


def _event(
    *,
    task_id: str = "demo/1",
    project: str = "demo",
    title: str = "Build the thing",
    actor_type: ActorType = ActorType.ROLE,
    actor_name: str = "worker",
    current_node: str = "do_work",
    current_node_kind: str = "work",
    work_status: str = "queued",
    commit_ref: str | None = None,
) -> TaskAssignmentEvent:
    return TaskAssignmentEvent(
        task_id=task_id,
        project=project,
        task_number=int(task_id.split("/", 1)[1]),
        title=title,
        current_node=current_node,
        current_node_kind=current_node_kind,
        actor_type=actor_type,
        actor_name=actor_name,
        work_status=work_status,
        priority="normal",
        transitioned_at=datetime.now(timezone.utc),
        transitioned_by="tester",
        commit_ref=commit_ref,
    )


# ---------------------------------------------------------------------------
# Role resolution
# ---------------------------------------------------------------------------


class TestRoleCandidates:
    def test_worker_expands_to_both_separators(self):
        assert role_candidate_names("worker", "demo") == [
            "worker-demo", "worker_demo",
        ]

    def test_reviewer_pins_to_pm_reviewer(self):
        assert role_candidate_names("reviewer", "ignored") == ["pm-reviewer"]

    def test_operator_pins_to_pm_operator(self):
        assert role_candidate_names("operator", "x") == ["pm-operator"]

    def test_heartbeat_supervisor_pins_to_pm_heartbeat(self):
        assert role_candidate_names("heartbeat-supervisor", "x") == ["pm-heartbeat"]
        # alias
        assert role_candidate_names("heartbeat", "x") == ["pm-heartbeat"]

    def test_triage(self):
        assert role_candidate_names("triage", "x") == ["pm-triage"]

    def test_critic_passes_through(self):
        assert role_candidate_names("critic_simplicity", "x") == ["critic_simplicity"]

    def test_unknown_role_yields_no_candidates(self):
        assert role_candidate_names("invented", "x") == []


class TestSessionRoleIndexResolve:
    def test_worker_prefers_dash_variant(self):
        svc = FakeSessionService(handles=[
            FakeHandle("worker-demo"),
            FakeHandle("worker_demo"),
        ])
        index = SessionRoleIndex(svc)
        handle = index.resolve(ActorType.ROLE, "worker", "demo")
        assert handle is not None
        assert handle.name == "worker-demo"

    def test_worker_falls_back_to_underscore(self):
        svc = FakeSessionService(handles=[FakeHandle("worker_demo")])
        index = SessionRoleIndex(svc)
        handle = index.resolve(ActorType.ROLE, "worker", "demo")
        assert handle is not None
        assert handle.name == "worker_demo"

    def test_reviewer(self):
        svc = FakeSessionService(handles=[FakeHandle("pm-reviewer")])
        index = SessionRoleIndex(svc)
        handle = index.resolve(ActorType.ROLE, "reviewer", "demo")
        assert handle is not None
        assert handle.name == "pm-reviewer"

    def test_agent_exact_name(self):
        svc = FakeSessionService(handles=[
            FakeHandle("polly"), FakeHandle("pm-reviewer"),
        ])
        index = SessionRoleIndex(svc)
        handle = index.resolve(ActorType.AGENT, "polly", "demo")
        assert handle is not None
        assert handle.name == "polly"

    def test_human_returns_none(self):
        svc = FakeSessionService(handles=[FakeHandle("pm-reviewer")])
        index = SessionRoleIndex(svc)
        assert index.resolve(ActorType.HUMAN, "reviewer", "demo") is None

    def test_no_matching_session_returns_none(self):
        svc = FakeSessionService(handles=[FakeHandle("worker-other")])
        index = SessionRoleIndex(svc)
        assert index.resolve(ActorType.ROLE, "worker", "demo") is None

    def test_multiple_matches_prefers_least_busy(self):
        """When both dash and underscore candidates exist, the resolver picks
        the one with fewer in-progress claims. We simulate a busy dash-variant
        and expect the underscore variant to win."""

        class FakeWork:
            def list_tasks(self, *, work_status=None, assignee=None, **kw):
                # Pretend worker-demo has 3 in-progress tasks, worker_demo 0.
                if assignee == "worker-demo":
                    return [type("T", (), {"work_status": type("S", (), {"value": "in_progress"})(), "assignee": "worker-demo"})() for _ in range(3)]
                return []

        svc = FakeSessionService(handles=[
            FakeHandle("worker-demo"),
            FakeHandle("worker_demo"),
        ])
        index = SessionRoleIndex(svc, work_service=FakeWork())
        handle = index.resolve(ActorType.ROLE, "worker", "demo")
        assert handle is not None
        assert handle.name == "worker_demo"


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------


class TestFormatPingForRole:
    def test_worker_ping_new_work(self):
        event = _event(current_node_kind="work", work_status="queued")
        text = format_ping_for_role(event)
        assert "New work" in text
        assert "[demo/1]" in text
        assert "pm task claim demo/1" in text

    def test_reviewer_ping(self):
        event = _event(
            actor_name="reviewer",
            current_node="human_review",
            current_node_kind="review",
            work_status="review",
            commit_ref="237dfb0",
        )
        text = format_ping_for_role(event)
        assert "Review needed" in text
        assert "(committed 237dfb0)" in text
        assert "pm task get demo/1" in text
        assert "pm task approve demo/1" in text
        assert "pm task reject demo/1" in text

    def test_resume_ping_for_in_progress_task(self):
        event = _event(
            current_node_kind="work",
            work_status="in_progress",
        )
        text = format_ping_for_role(event)
        assert "Resume work" in text


# ---------------------------------------------------------------------------
# Dedupe / escalation
# ---------------------------------------------------------------------------


@pytest.fixture
def state_store(tmp_path):
    store = StateStore(tmp_path / "state.db")
    yield store
    store.close()


class TestNotifyDedupe:
    def test_first_notification_sends(self, state_store):
        svc = FakeSessionService(handles=[FakeHandle("worker-demo")])
        services = _RuntimeServices(
            session_service=svc, state_store=state_store,
            work_service=None, project_root=Path("."),
        )
        outcome = notify(_event(), services=services)
        assert outcome["outcome"] == "sent"
        assert len(svc.sent) == 1
        assert svc.sent[0][0] == "worker-demo"

    def test_second_notification_within_30_min_deduped(self, state_store):
        svc = FakeSessionService(handles=[FakeHandle("worker-demo")])
        services = _RuntimeServices(
            session_service=svc, state_store=state_store,
            work_service=None, project_root=Path("."),
        )
        notify(_event(), services=services)
        outcome = notify(_event(), services=services)
        assert outcome["outcome"] == "deduped"
        assert len(svc.sent) == 1  # still just the first

    def test_past_throttle_resends(self, state_store):
        svc = FakeSessionService(handles=[FakeHandle("worker-demo")])
        services = _RuntimeServices(
            session_service=svc, state_store=state_store,
            work_service=None, project_root=Path("."),
        )
        # Send with a 0-second throttle to simulate "past the window".
        notify(_event(), services=services)
        outcome = notify(_event(), services=services, throttle_seconds=0)
        assert outcome["outcome"] == "sent"
        assert len(svc.sent) == 2


class TestNotifyEscalation:
    def test_no_matching_session_raises_alert(self, state_store):
        svc = FakeSessionService(handles=[])  # nobody live
        services = _RuntimeServices(
            session_service=svc, state_store=state_store,
            work_service=None, project_root=Path("."),
        )
        outcome = notify(_event(), services=services)
        assert outcome["outcome"] == "no_session"
        alerts = state_store.open_alerts()
        assert any(a.alert_type == f"no_session_for_assignment:demo/1" for a in alerts)
        # Message guides the user to the fix command.
        matching = [a for a in alerts if a.alert_type.endswith(":demo/1")]
        assert any("pm worker-start demo" in a.message for a in matching)


# ---------------------------------------------------------------------------
# Sweeper
# ---------------------------------------------------------------------------


class TestSweeper:
    def _make_services(self, tmp_path, session_handles):
        """Build a _RuntimeServices with a real work service + real state store
        but a fake session service and no config loading."""
        db = tmp_path / "work.db"
        state_db = tmp_path / "state.db"
        work = SQLiteWorkService(db_path=db)
        store = StateStore(state_db)
        svc = FakeSessionService(handles=list(session_handles))
        return work, store, svc

    def test_sweeper_picks_up_preexisting_queued_task(self, tmp_path, monkeypatch):
        work, store, svc = self._make_services(
            tmp_path, [FakeHandle("worker-proj")],
        )
        # Drain listeners so the create/queue path doesn't synchronously
        # send via an accidentally-registered subscriber.
        bus.clear_listeners()
        task = work.create(
            title="Preexisting work",
            description="Make stuff",
            type="task",
            project="proj",
            flow_template="standard",
            roles={"worker": "agent-1", "reviewer": "agent-2"},
            priority="normal",
        )
        work.queue(task.task_id, "pm")

        # Sweeper runs on its own resolution of runtime services; patch
        # the loader so it returns our harness.
        from pollypm.plugins_builtin.task_assignment_notify import handlers
        import pollypm.plugins_builtin.task_assignment_notify.resolver as resolver_mod

        def _fake_loader(*, config_path=None):
            return _RuntimeServices(
                session_service=svc,
                state_store=store,
                work_service=work,
                project_root=tmp_path,
            )

        monkeypatch.setattr(
            "pollypm.plugins_builtin.task_assignment_notify.handlers.sweep.load_runtime_services",
            _fake_loader,
        )

        result = task_assignment_sweep_handler({})
        assert result["outcome"] == "swept"
        assert result["considered"] >= 1
        assert result["by_outcome"].get("sent", 0) >= 1
        assert any(
            "New work" in text for _name, text in svc.sent
        )

    def test_sweeper_skips_already_notified_within_cooldown(
        self, tmp_path, monkeypatch,
    ):
        work, store, svc = self._make_services(
            tmp_path, [FakeHandle("worker-proj")],
        )
        bus.clear_listeners()
        task = work.create(
            title="Preexisting work",
            description="Desc",
            type="task",
            project="proj",
            flow_template="standard",
            roles={"worker": "agent-1", "reviewer": "agent-2"},
            priority="normal",
        )
        work.queue(task.task_id, "pm")

        # Prepopulate a recent notification row — the sweeper should skip.
        store.record_notification(
            session_name="worker-proj", task_id=task.task_id,
            project="proj", message="stub", delivery_status="sent",
        )

        def _fake_loader(*, config_path=None):
            return _RuntimeServices(
                session_service=svc, state_store=store,
                work_service=work, project_root=tmp_path,
            )

        monkeypatch.setattr(
            "pollypm.plugins_builtin.task_assignment_notify.handlers.sweep.load_runtime_services",
            _fake_loader,
        )

        result = task_assignment_sweep_handler({})
        assert result["by_outcome"].get("deduped", 0) >= 1
        assert len(svc.sent) == 0


# ---------------------------------------------------------------------------
# Integration: transition emits event -> session receives ping
# ---------------------------------------------------------------------------


class TestEndToEndTransition:
    def test_queue_fires_event_and_pings_worker(self, tmp_path):
        bus.clear_listeners()
        db = tmp_path / "work.db"
        work = SQLiteWorkService(db_path=db)

        events: list[TaskAssignmentEvent] = []
        bus.register_listener(events.append)

        task = work.create(
            title="Ship it",
            description="Implement feature X",
            type="task",
            project="proj",
            flow_template="standard",
            roles={"worker": "agent-1", "reviewer": "agent-2"},
            priority="normal",
        )
        work.queue(task.task_id, "pm")

        assert events, "Queue transition should emit a TaskAssignmentEvent"
        event = events[-1]
        assert event.task_id == task.task_id
        assert event.project == "proj"
        assert event.actor_type is ActorType.ROLE
        # The standard flow's start node is a worker role.
        assert event.actor_name == "worker"
        assert event.work_status == "queued"
        assert event.current_node_kind == "work"

        bus.clear_listeners()

    def test_human_node_does_not_emit(self, tmp_path):
        """A transition into a HUMAN review node should not ping a session."""
        bus.clear_listeners()
        db = tmp_path / "work.db"
        work = SQLiteWorkService(db_path=db)

        events: list[TaskAssignmentEvent] = []
        bus.register_listener(events.append)

        task = work.create(
            title="With human review",
            description="Needs a human signoff",
            type="task",
            project="proj",
            flow_template="standard",
            roles={"worker": "agent-1", "reviewer": "agent-2"},
            priority="normal",
            requires_human_review=True,
        )
        # Queue may still fire an event (worker role) — we only care that
        # no HUMAN-type event sneaks out when we hit review.
        work.queue(task.task_id, "pm", skip_gates=True)
        events.clear()

        # Drive the task to the review node. Standard flow: claim -> node_done -> review.
        work.claim(task.task_id, "agent-1")
        from pollypm.work.models import (
            Artifact, ArtifactKind, OutputType, WorkOutput,
        )
        out = WorkOutput(
            type=OutputType.CODE_CHANGE,
            summary="Implemented feature X",
            artifacts=[Artifact(
                kind=ArtifactKind.COMMIT, description="feat: X", ref="abc123",
            )],
        )
        work.node_done(task.task_id, "agent-1", work_output=out, skip_gates=True)

        # Events after reaching review: none should have actor_type == HUMAN.
        assert all(e.actor_type is not ActorType.HUMAN for e in events)

        bus.clear_listeners()


# ---------------------------------------------------------------------------
# Payload round-trip for JobQueue.enqueue
# ---------------------------------------------------------------------------


def test_event_to_payload_round_trip():
    ev = _event(
        actor_name="critic_simplicity",
        current_node_kind="work",
        commit_ref="abcdef0",
    )
    payload = event_to_payload(ev)
    # JSON-compatible primitives only.
    import json
    reserialized = json.loads(json.dumps(payload))
    assert reserialized["task_id"] == ev.task_id
    assert reserialized["actor_type"] == "role"
    assert reserialized["actor_name"] == "critic_simplicity"
    assert reserialized["commit_ref"] == "abcdef0"


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


def test_migration_creates_task_notifications(tmp_path):
    store = StateStore(tmp_path / "state.db")
    try:
        row = store.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='task_notifications'"
        ).fetchone()
        assert row is not None
        # Indexes present
        indexes = [
            r[0] for r in store.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='task_notifications'"
            ).fetchall()
        ]
        assert any("recent" in i for i in indexes)
        assert any("session_task" in i for i in indexes)
        # Round-trip a row.
        store.record_notification(
            session_name="worker-demo", task_id="demo/1",
            project="demo", message="hi", delivery_status="sent",
        )
        assert store.was_notified_within("worker-demo", "demo/1", 60)
        rows = store.recent_notifications(limit=10)
        assert rows and rows[0]["session_name"] == "worker-demo"
    finally:
        store.close()


def test_build_event_from_task_returns_none_for_human_node():
    """Direct coverage on the helper that decides whether to emit."""
    from pollypm.work.models import FlowNode, NodeType

    class _Task:
        task_id = "demo/1"
        project = "proj"
        task_number = 1
        title = "t"
        current_node_id = "review_node"
        priority = None
        work_status = None

    human_node = FlowNode(
        name="review", type=NodeType.REVIEW,
        actor_type=ActorType.HUMAN, actor_role="reviewer",
    )
    assert build_event_from_task(_Task(), human_node, transitioned_by="tester") is None
