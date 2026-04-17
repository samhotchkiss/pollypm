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
    # #246: optional per-session busy map. A session appearing in this
    # set reports ``is_turn_active -> True`` so the idle-gated sweeper
    # branch skips it.
    busy: set[str] = field(default_factory=set)

    def list(self) -> list[FakeHandle]:
        return list(self.handles)

    def send(self, name: str, text: str, *, press_enter: bool = True) -> None:
        if self.send_failure is not None:
            raise self.send_failure
        self.sent.append((name, text))

    def is_turn_active(self, name: str) -> bool:
        return name in self.busy


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


# ---------------------------------------------------------------------------
# #246 — sweeper in_progress branch
# ---------------------------------------------------------------------------


class TestSweeperInProgressBranch:
    """The sweeper must now pick up in_progress tasks whose worker
    session is idle, and skip those whose worker is actively turning."""

    def _make_services(self, tmp_path, session_handles, busy=()):
        db = tmp_path / "work.db"
        state_db = tmp_path / "state.db"
        work = SQLiteWorkService(db_path=db)
        store = StateStore(state_db)
        svc = FakeSessionService(
            handles=list(session_handles), busy=set(busy),
        )
        return work, store, svc

    def _queue_and_claim(self, work, project="proj", assignee="agent-1"):
        bus.clear_listeners()
        task = work.create(
            title="In-flight work",
            description="Already claimed before restart",
            type="task",
            project=project,
            flow_template="standard",
            roles={"worker": assignee, "reviewer": "agent-2"},
            priority="normal",
        )
        work.queue(task.task_id, "pm")
        work.claim(task.task_id, assignee)
        return task

    def test_sweeper_resume_pings_idle_in_progress_worker(
        self, tmp_path, monkeypatch,
    ):
        """Task is claimed (in_progress), worker session is idle → resume ping."""
        work, store, svc = self._make_services(
            tmp_path, [FakeHandle("worker-proj")], busy=(),
        )
        task = self._queue_and_claim(work)
        # Pretend the in-process listener already fired its original ping
        # a long time ago — outside the 30-min dedupe window — by leaving
        # the notifications table empty. The sweeper should emit fresh.

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
        assert result["outcome"] == "swept"
        assert result["by_outcome"].get("sent", 0) >= 1
        resume_messages = [t for _n, t in svc.sent if "Resume work" in t]
        assert resume_messages, f"expected a Resume ping, got {svc.sent!r}"
        assert f"[{task.task_id}]" in resume_messages[0]

    def test_sweeper_skips_in_progress_when_worker_is_busy(
        self, tmp_path, monkeypatch,
    ):
        """A worker actively turning must not get re-pinged."""
        work, store, svc = self._make_services(
            tmp_path,
            [FakeHandle("worker-proj")],
            busy={"worker-proj"},
        )
        self._queue_and_claim(work)

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
        # Busy-skip goes into a dedicated bucket, not "sent".
        assert result["by_outcome"].get("skipped_active_turn", 0) >= 1
        assert result["by_outcome"].get("sent", 0) == 0
        assert not any("Resume work" in t for _n, t in svc.sent)

    def test_sweeper_in_progress_respects_dedupe(
        self, tmp_path, monkeypatch,
    ):
        """A fresh notification row within the cooldown → no re-ping."""
        work, store, svc = self._make_services(
            tmp_path, [FakeHandle("worker-proj")], busy=(),
        )
        task = self._queue_and_claim(work)
        # Pre-populate a recent ping so the cooldown is hot.
        store.record_notification(
            session_name="worker-proj", task_id=task.task_id,
            project="proj", message="previous ping", delivery_status="sent",
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
# #246 — session.created listener (immediate resume ping path)
# ---------------------------------------------------------------------------


class TestSessionCreatedListener:
    """Creating a fresh session targeting an in_progress task fires an
    immediate resume ping — no sweeper wait."""

    def _install_fake_loader(self, monkeypatch, services):
        monkeypatch.setattr(
            "pollypm.plugins_builtin.task_assignment_notify.plugin.load_runtime_services",
            lambda *, config_path=None: services,
        )

    def test_session_created_fires_resume_ping_for_in_progress(
        self, tmp_path, monkeypatch,
    ):
        from pollypm.plugins_builtin.task_assignment_notify.plugin import (
            _session_created_listener,
        )
        from pollypm.session_services.base import SessionCreatedEvent

        bus.clear_listeners()
        db = tmp_path / "work.db"
        state_db = tmp_path / "state.db"
        work = SQLiteWorkService(db_path=db)
        store = StateStore(state_db)
        svc = FakeSessionService(handles=[FakeHandle("worker-proj")])

        task = work.create(
            title="Crash + restart",
            description="Resume me",
            type="task",
            project="proj",
            flow_template="standard",
            roles={"worker": "agent-1", "reviewer": "agent-2"},
            priority="normal",
        )
        work.queue(task.task_id, "pm")
        work.claim(task.task_id, "agent-1")

        services = _RuntimeServices(
            session_service=svc, state_store=store,
            work_service=work, project_root=tmp_path,
        )
        self._install_fake_loader(monkeypatch, services)

        event = SessionCreatedEvent(
            name="worker-proj", role="worker",
            project="proj", provider="claude",
        )
        _session_created_listener(event)

        assert svc.sent, "session.created listener should have fired a ping"
        assert any(
            "Resume work" in text and f"[{task.task_id}]" in text
            for _name, text in svc.sent
        )

    def test_session_created_dedupe_prevents_double_ping(
        self, tmp_path, monkeypatch,
    ):
        """A session.created event for an already-pinged session must
        respect the 30-min (session, task) dedupe."""
        from pollypm.plugins_builtin.task_assignment_notify.plugin import (
            _session_created_listener,
        )
        from pollypm.session_services.base import SessionCreatedEvent

        bus.clear_listeners()
        db = tmp_path / "work.db"
        state_db = tmp_path / "state.db"
        work = SQLiteWorkService(db_path=db)
        store = StateStore(state_db)
        svc = FakeSessionService(handles=[FakeHandle("worker-proj")])

        task = work.create(
            title="Crash + restart",
            description="Resume me",
            type="task",
            project="proj",
            flow_template="standard",
            roles={"worker": "agent-1", "reviewer": "agent-2"},
            priority="normal",
        )
        work.queue(task.task_id, "pm")
        work.claim(task.task_id, "agent-1")

        # Pre-populate a notification that's inside the 30-min window.
        store.record_notification(
            session_name="worker-proj", task_id=task.task_id,
            project="proj", message="original ping", delivery_status="sent",
        )

        services = _RuntimeServices(
            session_service=svc, state_store=store,
            work_service=work, project_root=tmp_path,
        )
        self._install_fake_loader(monkeypatch, services)

        event = SessionCreatedEvent(
            name="worker-proj", role="worker",
            project="proj", provider="claude",
        )
        _session_created_listener(event)

        # 30-min dedupe blocks the re-ping.
        assert not svc.sent, (
            "expected dedupe to suppress session.created re-ping, got %r" % svc.sent
        )

    def test_session_created_for_non_matching_session_is_noop(
        self, tmp_path, monkeypatch,
    ):
        """A session.created for a name that resolves to a different
        role's session (e.g. reviewer boot) shouldn't ping the worker
        task targeting ``worker-proj``."""
        from pollypm.plugins_builtin.task_assignment_notify.plugin import (
            _session_created_listener,
        )
        from pollypm.session_services.base import SessionCreatedEvent

        bus.clear_listeners()
        db = tmp_path / "work.db"
        state_db = tmp_path / "state.db"
        work = SQLiteWorkService(db_path=db)
        store = StateStore(state_db)
        # The session service reports both sessions live, but the new
        # session is the reviewer, not the worker.
        svc = FakeSessionService(handles=[
            FakeHandle("worker-proj"),
            FakeHandle("pm-reviewer"),
        ])

        task = work.create(
            title="Worker task",
            description="Real description here",
            type="task",
            project="proj",
            flow_template="standard",
            roles={"worker": "agent-1", "reviewer": "agent-2"},
            priority="normal",
        )
        work.queue(task.task_id, "pm")
        work.claim(task.task_id, "agent-1")

        services = _RuntimeServices(
            session_service=svc, state_store=store,
            work_service=work, project_root=tmp_path,
        )
        self._install_fake_loader(monkeypatch, services)

        event = SessionCreatedEvent(
            name="pm-reviewer", role="reviewer",
            project="proj", provider="claude",
        )
        _session_created_listener(event)

        # The worker task resolves to worker-proj, not pm-reviewer, so
        # no ping should fire for this event.
        assert not svc.sent


# ---------------------------------------------------------------------------
# #246 — session bus: register/dispatch plumbing
# ---------------------------------------------------------------------------


class TestTmuxServiceEmitsSessionCreated:
    """The TmuxSessionService must dispatch a SessionCreatedEvent via
    ``_emit_session_created``. We exercise the helper directly rather
    than driving ``create()`` (which requires a live tmux)."""

    def test_emit_publishes_to_session_bus(self, tmp_path):
        from pollypm.session_services.base import (
            SessionCreatedEvent,
            clear_session_listeners,
            register_session_listener,
        )
        from pollypm.session_services.tmux import TmuxSessionService

        class _Project:
            def __init__(self, root):
                self.root_dir = root
                self.name = "demo"

        class _Config:
            def __init__(self, root):
                self.project = _Project(root)

        clear_session_listeners()
        received: list[SessionCreatedEvent] = []
        register_session_listener(received.append)

        svc = TmuxSessionService(config=_Config(tmp_path), store=object())
        svc._emit_session_created(
            name="worker-demo",
            provider="claude",
            session_role="worker",
        )
        assert len(received) == 1
        ev = received[0]
        assert ev.name == "worker-demo"
        assert ev.role == "worker"
        assert ev.project == "demo"
        assert ev.provider == "claude"
        clear_session_listeners()

    def test_emit_tolerates_missing_project_name(self, tmp_path):
        """When the config shape doesn't expose ``name``, emit still
        fires with a best-effort empty string — we never raise out of
        ``create()``."""
        from pollypm.session_services.base import (
            SessionCreatedEvent,
            clear_session_listeners,
            register_session_listener,
        )
        from pollypm.session_services.tmux import TmuxSessionService

        class _Project:
            def __init__(self, root):
                self.root_dir = root

        class _Config:
            def __init__(self, root):
                self.project = _Project(root)

        clear_session_listeners()
        received: list[SessionCreatedEvent] = []
        register_session_listener(received.append)

        svc = TmuxSessionService(config=_Config(tmp_path), store=object())
        svc._emit_session_created(
            name="worker-demo",
            provider="claude",
            session_role=None,
        )
        assert len(received) == 1
        assert received[0].role == ""
        assert received[0].project == ""
        clear_session_listeners()


class TestSupervisorRestartScenario:
    """End-to-end #246: simulate the live russell/1 scenario.

    1. A task is claimed (in_progress) — worker session is running.
    2. Supervisor "restarts": the old session goes away and a fresh
       worker-<project> session boots.
    3. The new session's creation must trigger an immediate resume
       ping (session.created path) — no sweeper wait required.
    4. Separately, the sweeper must also catch this case for environments
       where the session.created hook doesn't fire.
    """

    def test_session_created_path_resumes_in_progress_task(
        self, tmp_path, monkeypatch,
    ):
        from pollypm.plugins_builtin.task_assignment_notify.plugin import (
            _session_created_listener,
        )
        from pollypm.session_services.base import SessionCreatedEvent

        bus.clear_listeners()
        db = tmp_path / "work.db"
        state_db = tmp_path / "state.db"
        work = SQLiteWorkService(db_path=db)
        store = StateStore(state_db)

        # Step 1: worker claims a task — in_progress.
        task = work.create(
            title="Ship russell/1",
            description="Implement the russell feature",
            type="task",
            project="russell",
            flow_template="standard",
            roles={"worker": "worker", "reviewer": "reviewer"},
            priority="normal",
        )
        work.queue(task.task_id, "pm")
        work.claim(task.task_id, "worker")

        # Step 2: supervisor restarts, fresh session appears.
        svc = FakeSessionService(handles=[FakeHandle("worker-russell")])
        services = _RuntimeServices(
            session_service=svc, state_store=store,
            work_service=work, project_root=tmp_path,
        )
        monkeypatch.setattr(
            "pollypm.plugins_builtin.task_assignment_notify.plugin.load_runtime_services",
            lambda *, config_path=None: services,
        )

        # Step 3: session.created fires.
        _session_created_listener(SessionCreatedEvent(
            name="worker-russell", role="worker",
            project="russell", provider="claude",
        ))

        # Assert the resume ping landed on the fresh session.
        assert svc.sent, "session.created should have fired a resume ping"
        name, text = svc.sent[-1]
        assert name == "worker-russell"
        assert "Resume work" in text
        assert f"[{task.task_id}]" in text
        assert "pm task get " in text

    def test_sweeper_fallback_resumes_in_progress_task(
        self, tmp_path, monkeypatch,
    ):
        """Covers the case where session.created didn't fire (older
        session service implementation). The sweeper must pick it up
        within one cycle."""
        bus.clear_listeners()
        db = tmp_path / "work.db"
        state_db = tmp_path / "state.db"
        work = SQLiteWorkService(db_path=db)
        store = StateStore(state_db)

        task = work.create(
            title="Ship russell/1",
            description="Implement the russell feature",
            type="task",
            project="russell",
            flow_template="standard",
            roles={"worker": "worker", "reviewer": "reviewer"},
            priority="normal",
        )
        work.queue(task.task_id, "pm")
        work.claim(task.task_id, "worker")

        svc = FakeSessionService(handles=[FakeHandle("worker-russell")])

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
        assert result["outcome"] == "swept"
        resume = [t for _n, t in svc.sent if "Resume work" in t]
        assert resume, f"sweeper missed the in_progress task — got {svc.sent!r}"
        assert f"[{task.task_id}]" in resume[0]


class TestSessionCreatedBus:
    def test_register_and_dispatch_delivers_event(self):
        from pollypm.session_services.base import (
            SessionCreatedEvent,
            clear_session_listeners,
            dispatch_session_event,
            register_session_listener,
        )

        clear_session_listeners()
        received: list[SessionCreatedEvent] = []
        register_session_listener(received.append)
        event = SessionCreatedEvent(
            name="worker-demo", role="worker",
            project="demo", provider="claude",
        )
        dispatch_session_event(event)
        assert received == [event]
        clear_session_listeners()

    def test_listener_exception_does_not_break_dispatch(self):
        from pollypm.session_services.base import (
            SessionCreatedEvent,
            clear_session_listeners,
            dispatch_session_event,
            register_session_listener,
        )

        clear_session_listeners()
        called: list[str] = []

        def _boom(_ev):
            raise RuntimeError("listener misbehaved")

        def _good(ev):
            called.append(ev.name)

        register_session_listener(_boom)
        register_session_listener(_good)
        dispatch_session_event(SessionCreatedEvent(
            name="x", role="worker", project="demo", provider="claude",
        ))
        assert called == ["x"]
        clear_session_listeners()

    def test_register_is_idempotent(self):
        from pollypm.session_services.base import (
            SessionCreatedEvent,
            clear_session_listeners,
            dispatch_session_event,
            register_session_listener,
        )

        clear_session_listeners()
        received: list[str] = []

        def _one(ev):
            received.append(ev.name)

        register_session_listener(_one)
        register_session_listener(_one)
        register_session_listener(_one)
        dispatch_session_event(SessionCreatedEvent(
            name="x", role="worker", project="demo", provider="claude",
        ))
        assert received == ["x"]
        clear_session_listeners()
