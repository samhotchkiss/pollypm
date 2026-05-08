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

import pytest

from pollypm.plugins_builtin.task_assignment_notify.handlers.notify import (
    event_to_payload,
)
from pollypm.plugins_builtin.task_assignment_notify.resolver import (
    _RuntimeServices,
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

    def test_reviewer_with_project_prepends_per_project(self):
        # #1011 — when a project is supplied the per-project candidates
        # come first so the resolver sees a ``reviewer_<project>``
        # session spawned by ``pm worker-start --role reviewer <project>``.
        # Singleton form stays as the fallback (#272).
        assert role_candidate_names("reviewer", "bikepath") == [
            "reviewer_bikepath", "reviewer-bikepath", "reviewer", "pm-reviewer",
        ]

    def test_reviewer_with_task_number_still_uses_reviewer_lane(self):
        # #1439 — a still-open per-task worker pane must not steal
        # review-node handoff pings from the long-lived reviewer lane.
        assert role_candidate_names("reviewer", "savethenovel", task_number=12) == [
            "reviewer_savethenovel",
            "reviewer-savethenovel",
            "reviewer",
            "pm-reviewer",
        ]

    def test_reviewer_without_project_pins_to_singleton(self):
        # Empty project key → singleton-only (legacy behaviour).
        assert role_candidate_names("reviewer", "") == ["reviewer", "pm-reviewer"]

    def test_operator_with_project_prepends_per_project(self):
        # #1011 — same as reviewer; singleton form is the fallback.
        assert role_candidate_names("operator", "x") == [
            "operator_x", "operator-x", "operator", "pm-operator",
        ]

    def test_heartbeat_supervisor_pins_to_pm_heartbeat(self):
        # #1011 — heartbeat is the per-workspace supervisor; per-project
        # candidates are still emitted for symmetry, but in practice
        # ``no_session`` never opens for heartbeat (it's bootstrapped by
        # the supervisor, not the auto-recovery sweep).
        assert role_candidate_names("heartbeat-supervisor", "x") == [
            "heartbeat-supervisor_x", "heartbeat-supervisor-x",
            "heartbeat", "pm-heartbeat",
        ]
        # alias
        assert role_candidate_names("heartbeat", "x") == [
            "heartbeat_x", "heartbeat-x", "heartbeat", "pm-heartbeat",
        ]

    def test_triage_with_project_prepends_per_project(self):
        # #1011.
        assert role_candidate_names("triage", "x") == [
            "triage_x", "triage-x", "triage", "pm-triage",
        ]

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

    def test_reviewer_ignores_matching_task_window(self):
        svc = FakeSessionService(handles=[
            FakeHandle("task-savethenovel-12"),
            FakeHandle("reviewer_savethenovel"),
        ])
        index = SessionRoleIndex(svc)
        handle = index.resolve(
            ActorType.ROLE,
            "reviewer",
            "savethenovel",
            task_number=12,
        )
        assert handle is not None
        assert handle.name == "reviewer_savethenovel"

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


class TestReviewerNotifyRouting:
    def test_review_ping_targets_reviewer_not_task_worker(self, state_store):
        svc = FakeSessionService(handles=[
            FakeHandle("task-savethenovel-12"),
            FakeHandle("reviewer_savethenovel"),
        ])
        services = _RuntimeServices(
            session_service=svc,
            state_store=state_store,
            work_service=None,
            project_root=Path("."),
        )
        event = _event(
            task_id="savethenovel/12",
            project="savethenovel",
            actor_name="reviewer",
            current_node="code_review",
            current_node_kind="review",
            work_status="review",
        )

        outcome = notify(event, services=services)

        assert outcome["outcome"] == "sent"
        assert outcome["session"] == "reviewer_savethenovel"
        assert svc.sent[0][0] == "reviewer_savethenovel"
        assert "Review needed" in svc.sent[0][1]


def test_load_runtime_services_uses_workspace_root_work_db(tmp_path, monkeypatch) -> None:
    """The sweeper scans the same workspace-root DB as default task CLI commands."""
    from types import SimpleNamespace

    from pollypm.plugins_builtin.task_assignment_notify.resolver import (
        load_runtime_services,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text("[project]\n")
    config = SimpleNamespace(
        project=SimpleNamespace(
            workspace_root=workspace,
            root_dir=tmp_path / "global-config-dir",
            state_db=tmp_path / "global-config-dir" / "state.db",
            tmux_session="pollypm",
        ),
        projects={},
        planner=SimpleNamespace(
            enforce_plan=True,
            plan_dir="docs/plan",
            auto_claim=True,
            max_concurrent_per_project=2,
        ),
    )
    opened: dict[str, Path] = {}

    class FakeWorkService:
        def __init__(self, *, db_path: Path, project_path: Path) -> None:
            opened["db_path"] = db_path
            opened["project_path"] = project_path

    monkeypatch.setattr("pollypm.config.load_config", lambda _path: config)
    monkeypatch.setattr("pollypm.storage.state.StateStore", lambda _path: object())
    monkeypatch.setattr(
        "pollypm.session_services.tmux.TmuxSessionService",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        "pollypm.work.sqlite_service.SQLiteWorkService",
        FakeWorkService,
    )

    services = load_runtime_services(config_path=config_path)

    assert opened["db_path"] == workspace / ".pollypm" / "state.db"
    assert opened["project_path"] == workspace
    assert services.project_root == workspace


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
        assert any(a.alert_type == "no_session_for_assignment:demo/1" for a in alerts)
        # Message guides the user to the cockpit surface AND to a CLI
        # recovery command (#953 — keep CLI hints alongside UI guidance).
        matching = [a for a in alerts if a.alert_type.endswith(":demo/1")]
        assert any("Open the task in Tasks" in a.message for a in matching)
        # Worker-role no-session alert should surface ``pm task claim``
        # as the per-task recovery path (not ``pm task approve`` — that
        # is reviewer-only).
        assert any("Try: pm task claim demo/1" in a.message for a in matching)
        assert all(
            "pm task approve" not in a.message for a in matching
        ), "worker-role alert must not suggest pm task approve"

    def test_reviewer_no_session_hint_points_to_review_ui(self, state_store):
        """#953 — reviewer-role no-session alerts must surface human
        Approve/Reject as the canonical path AND lead the ``Try:`` block
        with ``pm task approve`` for CLI-only operators."""
        svc = FakeSessionService(handles=[])  # nobody live
        services = _RuntimeServices(
            session_service=svc, state_store=state_store,
            work_service=None, project_root=Path("."),
        )
        outcome = notify(
            _event(actor_name="reviewer", current_node="review",
                   current_node_kind="review"),
            services=services,
        )
        assert outcome["outcome"] == "no_session"
        alerts = state_store.open_alerts()
        matching = [a for a in alerts if a.alert_type.endswith(":demo/1")]
        assert matching, "expected a per-task no_session_for_assignment alert"
        message = matching[0].message
        # UI-pointer copy stays.
        assert "Open the task in Tasks or Inbox" in message
        assert "Approve or Reject" in message
        # CLI hint is restored, with ``pm task approve`` listed FIRST
        # (canonical human-review path), followed by the long-running
        # session and per-task worker fallbacks.
        assert "Try: pm task approve demo/1" in message
        # ``Try:`` line must come before the worker-start / claim
        # fallbacks so ``pm task approve`` is unambiguously first.
        approve_idx = message.find("pm task approve demo/1")
        worker_start_idx = message.find("pm worker-start --role reviewer demo")
        claim_idx = message.find("pm task claim demo/1")
        assert approve_idx != -1
        assert worker_start_idx != -1 and approve_idx < worker_start_idx
        assert claim_idx != -1 and approve_idx < claim_idx


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

    def test_sweeper_recognizes_per_task_session_for_in_progress(
        self, tmp_path, monkeypatch,
    ):
        """#921: when an in_progress task has a live ``task-<proj>-<N>``
        session in the storage closet, the sweep must NOT raise
        ``no_session`` for it. The session counts as the worker for
        that ``(project, role=worker)`` lookup.
        """
        # Only the per-task session is "live" in the FakeSessionService.
        # Tasks numbered from 1 within a project's DB; we wire the
        # window name from the task's actual number so the test stays
        # honest about the naming contract regardless of allocation order.
        work, store, _ = self._make_services(tmp_path, [])
        bus.clear_listeners()
        task = work.create(
            title="Add charts",
            description="Implement",
            type="task",
            project="blackjack-trainer",
            flow_template="standard",
            roles={"worker": "agent-1", "reviewer": "agent-2"},
            priority="normal",
        )
        work.queue(task.task_id, "pm")
        # Move into in_progress so the sweep visits the in_progress branch.
        work.claim(task.task_id, "worker")
        live_window = f"task-{task.project}-{task.task_number}"
        svc = FakeSessionService(handles=[FakeHandle(live_window)])

        def _fake_loader(*, config_path=None):
            return _RuntimeServices(
                session_service=svc,
                state_store=store,
                work_service=work,
                project_root=tmp_path,
                msg_store=store,
            )

        monkeypatch.setattr(
            "pollypm.plugins_builtin.task_assignment_notify.handlers.sweep.load_runtime_services",
            _fake_loader,
        )

        result = task_assignment_sweep_handler({})
        assert result["outcome"] == "swept"
        # The sweep must NOT classify this task as ``no_session``.
        assert result["by_outcome"].get("no_session", 0) == 0
        # And the per-task session received the kickoff fan-out — not
        # the legacy ``worker-<project>`` name.
        assert any(
            name == f"task-{task.project}-{task.task_number}"
            for name, _text in svc.sent
        )
        # No sweep-level no_session alert was raised.
        sweep_alerts = [
            a for a in store.open_alerts()
            if a.alert_type == "no_session"
        ]
        assert sweep_alerts == []
        # No per-task no_session_for_assignment alert either.
        per_task_alerts = [
            a for a in store.open_alerts()
            if a.alert_type.startswith("no_session_for_assignment:")
        ]
        assert per_task_alerts == []

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
        assert row is None
        # Round-trip a row.
        store.record_notification(
            session_name="worker-demo", task_id="demo/1",
            project="demo", message="hi", delivery_status="sent",
        )
        assert store.was_notified_within("worker-demo", "demo/1", 60)
        rows = store.recent_notifications(limit=10)
        assert rows and rows[0]["session_name"] == "worker-demo"
        msg_rows = store.execute(
            "SELECT scope, recipient, sender FROM messages WHERE type = 'task_notification'"
        ).fetchall()
        assert msg_rows == [("worker-demo", "demo", "demo/1")]
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

    def test_sweeper_forced_kickoff_ignores_stale_normal_and_throttles_followup(
        self, tmp_path,
    ):
        """A forced kickoff must bypass poisoned normal rows, then throttle
        the ordinary recovery sweep that runs after kickoff_sent_at is
        stamped. This is the actual sweeper path behind #952."""
        work, store, svc = self._make_services(
            tmp_path, [FakeHandle("worker-proj")], busy=(),
        )
        task = self._queue_and_claim(work)
        store.record_notification(
            session_name="worker-proj",
            task_id=task.task_id,
            project="proj",
            message="poisoned pre-pane send",
            delivery_status="sent",
            execution_version=1,
        )

        from pollypm.plugins_builtin.task_assignment_notify.handlers.sweep import (
            _sweep_work_service,
        )

        services = _RuntimeServices(
            session_service=svc,
            state_store=store,
            work_service=work,
            project_root=tmp_path,
        )
        first_totals = {"considered": 0, "by_outcome": {}}
        second_totals = {"considered": 0, "by_outcome": {}}
        sweep_args = {
            "throttle_override": 300,
            "alerted_pairs": set(),
            "plan_missing_projects": set(),
            "plan_decisions": {},
        }

        _sweep_work_service(work, services, totals=first_totals, **sweep_args)
        _sweep_work_service(work, services, totals=second_totals, **sweep_args)

        assert first_totals["by_outcome"].get("forced_kickoff", 0) >= 1
        assert first_totals["by_outcome"].get("sent", 0) >= 1
        assert second_totals["by_outcome"].get("deduped", 0) >= 1
        assert second_totals["by_outcome"].get("sent", 0) == 0
        assert len(svc.sent) == 1

    def test_sweeper_skips_in_progress_when_worker_is_busy(
        self, tmp_path, monkeypatch,
    ):
        """A worker actively turning must not get re-pinged."""
        work, store, svc = self._make_services(
            tmp_path,
            [FakeHandle("worker-proj")],
            busy={"worker-proj"},
        )
        task = self._queue_and_claim(work)
        # #922: the worker has already received the initial kickoff
        # (otherwise the sweep would force-push past the busy gate to
        # break the bootstrap-race). Stamp the marker so the busy-gate
        # behaviour is what's actually under test.
        reloaded = work.get(task.task_id)
        work.mark_kickoff_sent(
            reloaded.project, reloaded.task_number, reloaded.current_node_id,
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
        # Pre-populate a recent ping so the cooldown is hot. The claim
        # above puts the task at ``implement`` with visit=1, so the
        # dedupe row must carry ``execution_version=1`` to match the
        # event the sweeper will build for this state (#279).
        store.record_notification(
            session_name="worker-proj", task_id=task.task_id,
            project="proj", message="previous ping", delivery_status="sent",
            execution_version=1,
        )
        # #922: also stamp kickoff_sent_at so the sweep's "force the
        # first push" branch doesn't bypass dedupe. The previous ping
        # we simulated above is exactly that delivery — without the
        # stamp the sweep would (correctly) treat the task as still
        # awaiting kickoff and re-fire past the dedupe.
        reloaded = work.get(task.task_id)
        work.mark_kickoff_sent(
            reloaded.project, reloaded.task_number, reloaded.current_node_id,
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
        # The claim puts the task at ``implement`` with visit=1, so the
        # dedupe row must carry ``execution_version=1`` to match the
        # replay event's identity (#279).
        store.record_notification(
            session_name="worker-proj", task_id=task.task_id,
            project="proj", message="original ping", delivery_status="sent",
            execution_version=1,
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


# ---------------------------------------------------------------------------
# #279 — reject-bounce unlocks retry ping via execution_version
# ---------------------------------------------------------------------------


class TestRejectBounceDedupe:
    """The 30-minute dedupe was originally keyed on ``(session, task)``
    only — a rejection that bounced the task back to ``implement`` v2
    never got its retry ping because the dedupe still saw the stale
    ``visit=1`` row as "already pinged". #279 keys the dedupe on
    ``(session, task, execution_version)`` so a fresh visit counts as a
    new ping opportunity."""

    def _make_services(self, tmp_path, session_handles, busy=()):
        db = tmp_path / "work.db"
        state_db = tmp_path / "state.db"
        work = SQLiteWorkService(db_path=db)
        store = StateStore(state_db)
        svc = FakeSessionService(
            handles=list(session_handles), busy=set(busy),
        )
        return work, store, svc

    def test_reject_bounce_unlocks_retry_ping(self, tmp_path):
        """Ping → reject → ping: the retry ping at visit=2 gets through
        even inside the 30-min window that throttled the visit=1 ping.

        This is the headline fix: the live scenario (russell/1 on
        e2e_auto_1776439172) where a rejected worker sat for 30 min
        waiting for the dedupe to expire."""
        from pollypm.work.models import (
            Artifact, ArtifactKind, OutputType, WorkOutput,
        )

        bus.clear_listeners()
        work, store, svc = self._make_services(
            tmp_path, [FakeHandle("worker-proj")],
        )
        services = _RuntimeServices(
            session_service=svc, state_store=store,
            work_service=work, project_root=tmp_path,
        )

        task = work.create(
            title="Build the thing",
            description="Implement feature X",
            type="task",
            project="proj",
            flow_template="standard",
            roles={"worker": "agent-1", "reviewer": "agent-2"},
            priority="normal",
        )
        # Claim puts the task at implement/visit=1.
        work.queue(task.task_id, "pm")
        work.claim(task.task_id, "agent-1")

        # First ping: lands.
        first_event = TaskAssignmentEvent(
            task_id=task.task_id, project="proj",
            task_number=task.task_number, title=task.title,
            current_node="implement", current_node_kind="work",
            actor_type=ActorType.ROLE, actor_name="worker",
            work_status="in_progress", priority="normal",
            transitioned_at=datetime.now(timezone.utc),
            transitioned_by="tester",
            execution_version=work.current_node_visit(
                "proj", task.task_number, "implement",
            ),
        )
        assert first_event.execution_version == 1
        first = notify(first_event, services=services)
        assert first["outcome"] == "sent"
        assert first["execution_version"] == 1
        assert len(svc.sent) == 1

        # Worker finishes -> review (visit=1 at code_review).
        out = WorkOutput(
            type=OutputType.CODE_CHANGE,
            summary="First attempt",
            artifacts=[Artifact(
                kind=ArtifactKind.COMMIT, description="impl", ref="abc",
            )],
        )
        work.node_done(task.task_id, "agent-1", work_output=out)
        # Reviewer rejects -> bounce to implement/visit=2.
        work.reject(task.task_id, "agent-2", "try again")

        second_visit = work.current_node_visit(
            "proj", task.task_number, "implement",
        )
        assert second_visit == 2

        # Second ping carries the new execution_version — dedupe must
        # NOT suppress it even though we're well inside the 30-min
        # window that throttled the first ping.
        second_event = TaskAssignmentEvent(
            task_id=task.task_id, project="proj",
            task_number=task.task_number, title=task.title,
            current_node="implement", current_node_kind="work",
            actor_type=ActorType.ROLE, actor_name="worker",
            work_status="in_progress", priority="normal",
            transitioned_at=datetime.now(timezone.utc),
            transitioned_by="tester",
            execution_version=second_visit,
        )
        second = notify(second_event, services=services)
        assert second["outcome"] == "sent", (
            f"reject-bounce retry ping was suppressed: {second!r}"
        )
        assert second["execution_version"] == 2
        assert len(svc.sent) == 2

    def test_same_state_within_window_still_dedupes(self, state_store):
        """Ping at visit=N → ping again at visit=N within the window →
        suppressed. The version-aware dedupe must still catch pure
        duplicates — we didn't just remove the throttle, we refined its
        key."""
        svc = FakeSessionService(handles=[FakeHandle("worker-demo")])
        services = _RuntimeServices(
            session_service=svc, state_store=state_store,
            work_service=None, project_root=Path("."),
        )
        event = _event()
        # _event() defaults to execution_version=0, matching the
        # pre-#279 dedupe semantics for the "no work service" harness.
        first = notify(event, services=services)
        assert first["outcome"] == "sent"
        second = notify(event, services=services)
        assert second["outcome"] == "deduped"
        assert second["execution_version"] == 0
        assert len(svc.sent) == 1

    def test_past_throttle_same_state_resends(self, state_store):
        """Time-based dedupe still works for unchanged state. A ping at
        version=0 that expires the window must re-send — we must not
        have accidentally replaced the time window with a per-version
        "ping exactly once" behaviour."""
        svc = FakeSessionService(handles=[FakeHandle("worker-demo")])
        services = _RuntimeServices(
            session_service=svc, state_store=state_store,
            work_service=None, project_root=Path("."),
        )
        notify(_event(), services=services)
        # throttle_seconds=0 models "we're past the 30-min window".
        outcome = notify(_event(), services=services, throttle_seconds=0)
        assert outcome["outcome"] == "sent"
        assert len(svc.sent) == 2

    def test_sweeper_rebuilds_events_with_current_visit(
        self, tmp_path, monkeypatch,
    ):
        """The sweeper's synthetic event must carry the current visit so
        its dedupe lines up with the live work service state. This is
        the path that runs on the @every 30s cadence and has to catch
        the reject-bounce case when the in-process listener missed the
        transition."""
        from pollypm.work.models import (
            Artifact, ArtifactKind, OutputType, WorkOutput,
        )
        from pollypm.plugins_builtin.task_assignment_notify.handlers.sweep import (
            _build_event_for_task,
        )

        bus.clear_listeners()
        work, store, svc = self._make_services(
            tmp_path, [FakeHandle("worker-proj")],
        )

        task = work.create(
            title="Build the thing",
            description="Implement feature X",
            type="task",
            project="proj",
            flow_template="standard",
            roles={"worker": "agent-1", "reviewer": "agent-2"},
            priority="normal",
        )
        work.queue(task.task_id, "pm")
        work.claim(task.task_id, "agent-1")

        # Visit 1 at implement.
        t1 = work.get(task.task_id)
        ev1 = _build_event_for_task(work, t1)
        assert ev1 is not None
        assert ev1.execution_version == 1

        # Advance to review, then bounce back.
        out = WorkOutput(
            type=OutputType.CODE_CHANGE,
            summary="First attempt",
            artifacts=[Artifact(
                kind=ArtifactKind.COMMIT, description="impl", ref="abc",
            )],
        )
        work.node_done(task.task_id, "agent-1", work_output=out)
        work.reject(task.task_id, "agent-2", "try again")

        t2 = work.get(task.task_id)
        ev2 = _build_event_for_task(work, t2)
        assert ev2 is not None
        assert ev2.execution_version == 2, (
            f"sweeper rebuild missed the visit bump: {ev2.execution_version}"
        )

    def test_pre_migration_row_dedupes_default_version_event(self, state_store):
        """An event rebuilt via a work service that can't compute a
        visit (bare test double, failure path) emits ``version=0``. A
        notification row created before #279 (column DEFAULT 0) must
        still dedupe that event. This is the backward-compat contract
        the spec called out."""
        svc = FakeSessionService(handles=[FakeHandle("worker-demo")])
        services = _RuntimeServices(
            session_service=svc, state_store=state_store,
            work_service=None, project_root=Path("."),
        )
        # Pre-populate a "pre-migration" row (execution_version omitted,
        # so it lands as the column DEFAULT 0).
        state_store.record_notification(
            session_name="worker-demo", task_id="demo/1",
            project="demo", message="legacy ping", delivery_status="sent",
            # execution_version not passed — default 0, matching a row
            # back-filled by migration 12.
        )
        outcome = notify(_event(), services=services)
        assert outcome["outcome"] == "deduped", (
            f"pre-migration dedupe compat broken: {outcome!r}"
        )
        assert len(svc.sent) == 0

    def test_migration_adds_execution_version_column(self, tmp_path):
        """Notification dedupe stores ``execution_version`` in messages payload."""
        store = StateStore(tmp_path / "state.db")
        try:
            assert store.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='task_notifications'"
            ).fetchone() is None
            # Round-trip with + without the column: both forms work,
            # and default rows dedupe at version=0.
            store.record_notification(
                session_name="worker-demo", task_id="demo/1",
                project="demo", message="default", delivery_status="sent",
            )
            assert store.was_notified_within("worker-demo", "demo/1", 60)
            # Explicit version round-trip.
            store.record_notification(
                session_name="worker-demo", task_id="demo/2",
                project="demo", message="v3", delivery_status="sent",
                execution_version=3,
            )
            assert store.was_notified_within(
                "worker-demo", "demo/2", 60, execution_version=3,
            )
            # Different version is NOT matched by the dedupe query.
            assert not store.was_notified_within(
                "worker-demo", "demo/2", 60, execution_version=2,
            )
            # The recent_notifications readback surfaces the version so
            # downstream tooling can display it.
            rows = store.recent_notifications(task_id="demo/2")
            assert rows and rows[0]["execution_version"] == 3
            payload = store.execute(
                "SELECT json_extract(payload_json, '$.execution_version') "
                "FROM messages WHERE type = 'task_notification' AND sender = 'demo/2'"
            ).fetchone()
            assert payload == (3,)
        finally:
            store.close()

    def test_migration_upgrades_pre_existing_v11_database(self, tmp_path):
        """A database that existed before #279 (schema v11) with
        ``task_notifications`` rows must upgrade cleanly to the unified
        ``messages`` table with version payload back-filled to 0."""
        import sqlite3
        p = tmp_path / "legacy.db"
        # Hand-build a v11-shaped DB (no execution_version column).
        conn = sqlite3.connect(p)
        conn.executescript(
            """
            CREATE TABLE schema_version (
                version INTEGER NOT NULL,
                description TEXT NOT NULL,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE task_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_name TEXT NOT NULL,
                task_id TEXT NOT NULL,
                project TEXT NOT NULL DEFAULT '',
                notified_at TEXT NOT NULL,
                delivery_status TEXT NOT NULL DEFAULT 'sent',
                message TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO schema_version VALUES
                (11, 'v11 baseline', '2026-01-01T00:00:00+00:00');
            INSERT INTO task_notifications
                (session_name, task_id, project, notified_at,
                 delivery_status, message)
            VALUES
                ('worker-legacy', 'legacy/1', 'legacy',
                 '2026-04-17T00:00:00+00:00', 'sent', 'legacy ping');
            """
        )
        conn.commit()
        conn.close()

        # Open via StateStore — the migration runner must upgrade in
        # place without losing data.
        store = StateStore(p)
        try:
            assert store.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='task_notifications'"
            ).fetchone() is None
            rows = store.execute(
                "SELECT scope, sender, json_extract(payload_json, '$.execution_version') "
                "FROM messages WHERE type = 'task_notification'"
            ).fetchall()
            assert rows == [("worker-legacy", "legacy/1", 0)], (
                f"legacy row did not migrate to messages with version=0: {rows!r}"
            )
            version = store.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()[0]
            # The v11 fixture must upgrade through the retirement pass
            # to the current schema head.
            assert version >= 15
        finally:
            store.close()

    def test_event_payload_round_trip_preserves_version(self):
        """The notify job payload must round-trip the version so a
        sweeper-enqueued job handed off to a worker runner dedupes on
        the same key the in-process path uses."""
        from pollypm.plugins_builtin.task_assignment_notify.handlers.notify import (
            _event_from_payload, event_to_payload,
        )
        ev = TaskAssignmentEvent(
            task_id="proj/5", project="proj", task_number=5,
            title="x", current_node="implement", current_node_kind="work",
            actor_type=ActorType.ROLE, actor_name="worker",
            work_status="in_progress", priority="normal",
            transitioned_at=datetime.now(timezone.utc),
            transitioned_by="tester",
            execution_version=4,
        )
        payload = event_to_payload(ev)
        assert payload["execution_version"] == 4
        # Serializer is JSON-compatible.
        import json
        round = json.loads(json.dumps(payload))
        restored = _event_from_payload(round)
        assert restored.execution_version == 4


# ---------------------------------------------------------------------------
# #921 — per-task worker session recognition
# ---------------------------------------------------------------------------


class TestPerTaskWorkerRecognition:
    """The post-#919 ``task-<project>-<N>`` window must count as the
    worker session for ``(project=<project>, role=worker)`` for that
    task. Without this, ``pm task claim`` spawns a worker that the
    notify path can't see, ``no_session`` alerts fire spuriously, and
    the kickoff message never reaches the spawned pane.
    """

    def test_role_candidates_prepend_per_task_window(self):
        # Worker role with a task_number gets the canonical per-task
        # form first, then the legacy long-lived names as fallbacks.
        assert role_candidate_names(
            "worker", "blackjack-trainer", task_number=3,
        ) == [
            "task-blackjack-trainer-3",
            "worker-blackjack-trainer",
            "worker_blackjack-trainer",
        ]

    def test_role_candidates_no_task_number_unchanged(self):
        # Without a task_number the legacy candidate list is preserved
        # — nothing else needs to know about per-task naming.
        assert role_candidate_names("worker", "demo") == [
            "worker-demo", "worker_demo",
        ]

    def test_resolver_picks_task_window_over_legacy_worker(self):
        # When both legacy and per-task sessions are present, the
        # resolver prefers the per-task one.
        svc = FakeSessionService(handles=[
            FakeHandle("worker-blackjack-trainer"),
            FakeHandle("task-blackjack-trainer-3"),
        ])
        index = SessionRoleIndex(svc)
        handle = index.resolve(
            ActorType.ROLE, "worker", "blackjack-trainer", task_number=3,
        )
        assert handle is not None
        assert handle.name == "task-blackjack-trainer-3"

    def test_resolver_falls_back_to_legacy_worker_when_no_task_window(self):
        # A project that still runs a long-lived worker (booktalk-style)
        # is unaffected: when no per-task window matches, the legacy
        # ``worker-<project>`` resolves as before.
        svc = FakeSessionService(handles=[FakeHandle("worker-booktalk")])
        index = SessionRoleIndex(svc)
        handle = index.resolve(
            ActorType.ROLE, "worker", "booktalk", task_number=7,
        )
        assert handle is not None
        assert handle.name == "worker-booktalk"

    def test_resolver_does_not_match_sibling_task_window(self):
        # The candidate is task-<project>-<N> exactly. A different
        # task_number for the same project must NOT match.
        svc = FakeSessionService(handles=[
            FakeHandle("task-blackjack-trainer-3"),
        ])
        index = SessionRoleIndex(svc)
        handle = index.resolve(
            ActorType.ROLE, "worker", "blackjack-trainer", task_number=4,
        )
        assert handle is None


class TestPerTaskWorkerFulfillsNonWorkerRoles:
    """#1057 — per-task workers (``task-<project>-<N>``) fulfill ANY
    role assignment for their specific task, not just ``worker``. When
    the planner spawns a critic subtask the task lands in_progress with
    assignee ``critic_simplicity`` and a ``task-<project>-<N>`` window
    is doing the work; the role-assignment resolver must accept that
    window or ``no_session_for_assignment`` fires spuriously while the
    critic is actively running.
    """

    def test_critic_role_resolves_to_per_task_window(self):
        # The exact symptom from #1057: task ``polly_remote/24`` has a
        # critic_simplicity assignee and the per-task worker window
        # ``task-polly_remote-24`` is alive. Resolution must succeed.
        svc = FakeSessionService(handles=[
            FakeHandle("task-polly_remote-24"),
        ])
        index = SessionRoleIndex(svc)
        handle = index.resolve(
            ActorType.ROLE, "critic_simplicity", "polly_remote", task_number=24,
        )
        assert handle is not None
        assert handle.name == "task-polly_remote-24"

    def test_role_candidates_for_critic_includes_per_task_window(self):
        from pollypm.work.task_assignment import role_candidate_names

        names = role_candidate_names(
            "critic_simplicity", "polly_remote", task_number=24,
        )
        # Per-task window comes first; persona-named fallback retained.
        assert names[0] == "task-polly_remote-24"
        assert "critic_simplicity" in names

    def test_long_running_role_session_still_resolves(self):
        # If a workspace did run a session named after the persona
        # (legacy / hand-spawned), the resolver still finds it via the
        # exact-name fallback.
        svc = FakeSessionService(handles=[FakeHandle("critic_simplicity")])
        index = SessionRoleIndex(svc)
        handle = index.resolve(
            ActorType.ROLE, "critic_simplicity", "polly_remote", task_number=24,
        )
        assert handle is not None
        assert handle.name == "critic_simplicity"

    def test_no_worker_at_all_returns_none(self):
        # No per-task window, no persona-named session — resolver
        # returns None and the alert path fires correctly.
        svc = FakeSessionService(handles=[FakeHandle("worker-other-project")])
        index = SessionRoleIndex(svc)
        handle = index.resolve(
            ActorType.ROLE, "critic_simplicity", "polly_remote", task_number=24,
        )
        assert handle is None

    def test_legacy_role_session_resolves_for_arbitrary_role(self):
        # ``role_candidate_names`` for an unknown role with a
        # task_number now returns the per-task candidate. Without one
        # in tmux the resolver returns None.
        svc = FakeSessionService(handles=[
            FakeHandle("task-polly_remote-24"),
        ])
        index = SessionRoleIndex(svc)
        handle = index.resolve(
            ActorType.ROLE, "synthesizer", "polly_remote", task_number=24,
        )
        assert handle is not None
        assert handle.name == "task-polly_remote-24"

    def test_notify_no_alert_when_per_task_window_matches_critic(
        self, state_store,
    ):
        # End-to-end: a critic-role assignment with a live per-task
        # window should NOT raise the no_session_for_assignment alert.
        svc = FakeSessionService(handles=[
            FakeHandle("task-polly_remote-24"),
        ])
        services = _RuntimeServices(
            session_service=svc, state_store=state_store,
            work_service=None, project_root=Path("."),
        )
        event = TaskAssignmentEvent(
            task_id="polly_remote/24",
            project="polly_remote",
            task_number=24,
            title="Critic: simplicity review",
            current_node="critique",
            current_node_kind="work",
            actor_type=ActorType.ROLE,
            actor_name="critic_simplicity",
            work_status="in_progress",
            priority="normal",
            transitioned_at=datetime.now(timezone.utc),
            transitioned_by="tester",
        )
        outcome = notify(event, services=services)
        assert outcome["outcome"] == "sent"
        assert outcome["session"] == "task-polly_remote-24"
        alerts = state_store.open_alerts()
        per_task = [
            a for a in alerts
            if a.alert_type == "no_session_for_assignment:polly_remote/24"
        ]
        assert per_task == []

    def test_notify_alert_fires_when_no_session_at_all(self, state_store):
        # No live session of any kind → escalate to alert as before.
        svc = FakeSessionService(handles=[])
        services = _RuntimeServices(
            session_service=svc, state_store=state_store,
            work_service=None, project_root=Path("."),
        )
        event = TaskAssignmentEvent(
            task_id="polly_remote/24",
            project="polly_remote",
            task_number=24,
            title="Critic: simplicity review",
            current_node="critique",
            current_node_kind="work",
            actor_type=ActorType.ROLE,
            actor_name="critic_simplicity",
            work_status="in_progress",
            priority="normal",
            transitioned_at=datetime.now(timezone.utc),
            transitioned_by="tester",
        )
        outcome = notify(event, services=services)
        assert outcome["outcome"] == "no_session"
        alerts = state_store.open_alerts()
        per_task = [
            a for a in alerts
            if a.alert_type == "no_session_for_assignment:polly_remote/24"
        ]
        assert len(per_task) == 1
        # #1057 — alert hint should NOT suggest the bogus
        # ``pm worker-start --role critic_simplicity`` command.
        message = per_task[0].message
        assert "pm worker-start --role critic_simplicity" not in message
        # And it should reference the per-task worker explanation.
        assert "task-polly_remote-24" in message or "#1057" in message


class TestSingletonRolesNeverRouteToPerTaskPane:
    """Regression — per-task panes (``task-<project>-<N>``) must never
    fulfill singleton-named control roles (reviewer / operator / triage /
    heartbeat). The per-task pane is the AUTHOR of the work; routing a
    reviewer-kickoff prompt to it would land the prompt on the worker
    that just produced the diff.

    Live symptom that motivated this guard: savethenovel/15's per-task
    worker pane received 5 misrouted reviewer-kickoff prompts (the
    reviewer-singleton resolution short-circuited on the live per-task
    pane via ``role_candidate_names``'s singleton-branch prepend) and
    eventually auth-wedged on "Not logged in · /login" from the
    repeated noise.
    """

    def test_reviewer_kickoff_does_not_route_to_per_task_worker(self):
        # Reviewer kickoff for savethenovel/15 must route to the
        # dedicated reviewer session, never to the per-task worker pane
        # that owns the task. The per-task worker is the author of the
        # work and cannot review it.
        svc = FakeSessionService(handles=[
            FakeHandle("task-savethenovel-15"),
            FakeHandle("reviewer_savethenovel"),
        ])
        index = SessionRoleIndex(svc)
        handle = index.resolve(
            ActorType.ROLE, "reviewer", "savethenovel", task_number=15,
        )
        assert handle is not None
        assert handle.name == "reviewer_savethenovel"

    def test_reviewer_kickoff_falls_back_to_singleton_when_no_per_project(self):
        # No per-project ``reviewer_<project>`` session live — fall
        # through to the legacy singleton, NOT to the per-task pane.
        svc = FakeSessionService(handles=[
            FakeHandle("task-savethenovel-15"),
            FakeHandle("pm-reviewer"),
        ])
        index = SessionRoleIndex(svc)
        handle = index.resolve(
            ActorType.ROLE, "reviewer", "savethenovel", task_number=15,
        )
        assert handle is not None
        assert handle.name == "pm-reviewer"

    def test_reviewer_kickoff_returns_none_when_only_per_task_pane_alive(self):
        # No reviewer session of any kind, even with a live per-task
        # pane — resolver returns None so the alert path fires correctly
        # rather than misrouting to the worker that authored the diff.
        svc = FakeSessionService(handles=[
            FakeHandle("task-savethenovel-15"),
        ])
        index = SessionRoleIndex(svc)
        handle = index.resolve(
            ActorType.ROLE, "reviewer", "savethenovel", task_number=15,
        )
        assert handle is None

    def test_operator_kickoff_does_not_route_to_per_task_worker(self):
        svc = FakeSessionService(handles=[
            FakeHandle("task-savethenovel-15"),
            FakeHandle("pm-operator"),
        ])
        index = SessionRoleIndex(svc)
        handle = index.resolve(
            ActorType.ROLE, "operator", "savethenovel", task_number=15,
        )
        assert handle is not None
        assert handle.name == "pm-operator"

    def test_triage_kickoff_does_not_route_to_per_task_worker(self):
        svc = FakeSessionService(handles=[
            FakeHandle("task-savethenovel-15"),
            FakeHandle("pm-triage"),
        ])
        index = SessionRoleIndex(svc)
        handle = index.resolve(
            ActorType.ROLE, "triage", "savethenovel", task_number=15,
        )
        assert handle is not None
        assert handle.name == "pm-triage"

    def test_heartbeat_kickoff_does_not_route_to_per_task_worker(self):
        svc = FakeSessionService(handles=[
            FakeHandle("task-savethenovel-15"),
            FakeHandle("pm-heartbeat"),
        ])
        index = SessionRoleIndex(svc)
        handle = index.resolve(
            ActorType.ROLE, "heartbeat", "savethenovel", task_number=15,
        )
        assert handle is not None
        assert handle.name == "pm-heartbeat"

    def test_role_candidates_for_reviewer_omits_per_task_pane(self):
        # Pure candidate-list shape check — per-task pane must not be
        # in the singleton-role candidate list at all, regardless of
        # what's live in tmux.
        names = role_candidate_names(
            "reviewer", "savethenovel", task_number=15,
        )
        assert "task-savethenovel-15" not in names
        # And the legitimate routing targets are still present.
        assert names == [
            "reviewer_savethenovel", "reviewer-savethenovel",
            "reviewer", "pm-reviewer",
        ]

    def test_role_candidates_for_operator_omits_per_task_pane(self):
        names = role_candidate_names("operator", "x", task_number=15)
        assert "task-x-15" not in names
        assert names == ["operator_x", "operator-x", "operator", "pm-operator"]

    def test_role_candidates_for_triage_omits_per_task_pane(self):
        names = role_candidate_names("triage", "x", task_number=15)
        assert "task-x-15" not in names

    def test_role_candidates_for_heartbeat_omits_per_task_pane(self):
        names = role_candidate_names("heartbeat", "x", task_number=15)
        assert "task-x-15" not in names

    # --- pin the original #1057 behaviour so a future "clean up"
    # doesn't undo the per-task-pane fulfillment for worker / critic_*.

    def test_worker_role_still_resolves_to_per_task_pane(self):
        # #1057 (still load-bearing): a per-task pane fulfills the
        # ``worker`` role for that task. Don't regress.
        svc = FakeSessionService(handles=[
            FakeHandle("task-savethenovel-15"),
        ])
        index = SessionRoleIndex(svc)
        handle = index.resolve(
            ActorType.ROLE, "worker", "savethenovel", task_number=15,
        )
        assert handle is not None
        assert handle.name == "task-savethenovel-15"

    def test_critic_role_still_resolves_to_per_task_pane(self):
        # #1057 (still load-bearing): a per-task pane fulfills
        # ``critic_*`` roles for that task. Don't regress.
        svc = FakeSessionService(handles=[
            FakeHandle("task-savethenovel-15"),
        ])
        index = SessionRoleIndex(svc)
        handle = index.resolve(
            ActorType.ROLE, "critic_simplicity", "savethenovel", task_number=15,
        )
        assert handle is not None
        assert handle.name == "task-savethenovel-15"

    def test_role_candidates_for_worker_still_includes_per_task_pane(self):
        names = role_candidate_names("worker", "savethenovel", task_number=15)
        assert names[0] == "task-savethenovel-15"

    def test_role_candidates_for_critic_still_includes_per_task_pane(self):
        names = role_candidate_names(
            "critic_simplicity", "savethenovel", task_number=15,
        )
        assert names[0] == "task-savethenovel-15"


class TestNotifyRoutesToPerTaskSession:
    """The work-push contract: when a task is in_progress with a fresh
    per-task session live, ``notify()`` sends the kickoff message to
    the per-task session — not the legacy ``worker-<project>`` name."""

    def test_notify_sends_to_task_window_for_in_progress(self, state_store):
        svc = FakeSessionService(handles=[
            FakeHandle("task-blackjack-trainer-3"),
        ])
        services = _RuntimeServices(
            session_service=svc, state_store=state_store,
            work_service=None, project_root=Path("."),
        )
        event = TaskAssignmentEvent(
            task_id="blackjack-trainer/3",
            project="blackjack-trainer",
            task_number=3,
            title="Add charts",
            current_node="implement",
            current_node_kind="work",
            actor_type=ActorType.ROLE,
            actor_name="worker",
            work_status="in_progress",
            priority="normal",
            transitioned_at=datetime.now(timezone.utc),
            transitioned_by="tester",
        )
        outcome = notify(event, services=services)
        assert outcome["outcome"] == "sent"
        assert outcome["session"] == "task-blackjack-trainer-3"
        assert len(svc.sent) == 1
        target, message = svc.sent[0]
        assert target == "task-blackjack-trainer-3"
        # Kickoff payload mirrors the legacy long-lived worker — task
        # description + the resume pointer.
        assert "Resume work" in message
        assert "[blackjack-trainer/3]" in message

    def test_notify_does_not_raise_no_session_when_per_task_lives(
        self, state_store,
    ):
        # Even though no ``worker-<project>`` session is registered,
        # the live ``task-<project>-<N>`` window must satisfy the
        # ``(project, role=worker)`` lookup so the notify path
        # succeeds without escalating ``no_session_for_assignment``.
        svc = FakeSessionService(handles=[
            FakeHandle("task-blackjack-trainer-3"),
        ])
        services = _RuntimeServices(
            session_service=svc, state_store=state_store,
            work_service=None, project_root=Path("."),
        )
        event = TaskAssignmentEvent(
            task_id="blackjack-trainer/3",
            project="blackjack-trainer",
            task_number=3,
            title="Add charts",
            current_node="implement",
            current_node_kind="work",
            actor_type=ActorType.ROLE,
            actor_name="worker",
            work_status="in_progress",
            priority="normal",
            transitioned_at=datetime.now(timezone.utc),
            transitioned_by="tester",
        )
        outcome = notify(event, services=services)
        assert outcome["outcome"] == "sent"
        alerts = state_store.open_alerts()
        per_task = [
            a for a in alerts
            if a.alert_type == "no_session_for_assignment:blackjack-trainer/3"
        ]
        assert per_task == []

    def test_tmux_session_service_list_synthesizes_per_task_handles(
        self, tmp_path,
    ):
        """The session service must surface ``task-<project>-<N>``
        windows that live in the storage closet but aren't recorded in
        the static StateStore sessions table. Without this, the
        SessionRoleIndex (which reads ``session_service.list()``) can't
        find the per-task worker even though tmux knows about it.
        """
        from types import SimpleNamespace

        from pollypm.session_services.tmux import TmuxSessionService
        from pollypm.tmux.client import TmuxWindow

        class FakeTmux:
            def list_all_windows(self):
                return [
                    TmuxWindow(
                        session="pollypm-storage-closet",
                        index=1,
                        name="task-blackjack-trainer-3",
                        active=True,
                        pane_id="%42",
                        pane_current_command="claude",
                        pane_current_path="/tmp/worktree",
                        pane_dead=False,
                    ),
                    TmuxWindow(
                        session="pollypm-storage-closet",
                        index=2,
                        name="pm-operator",
                        active=False,
                        pane_id="%43",
                        pane_current_command="claude",
                        pane_current_path="/tmp/operator",
                        pane_dead=False,
                    ),
                ]

        class FakeStore:
            def list_sessions(self):
                return []

        config = SimpleNamespace(
            project=SimpleNamespace(
                tmux_session="pollypm",
                snapshots_dir=tmp_path / "snapshots",
                root_dir=tmp_path,
            ),
            sessions={},
        )
        service = TmuxSessionService(config=config, store=FakeStore())
        service.tmux = FakeTmux()
        # Disable cockpit-mounted override path for this isolated test.
        service._mounted_window_override = lambda: None

        handles = service.list()
        names = {h.name for h in handles}
        # The per-task window appears as a synthetic handle.
        assert "task-blackjack-trainer-3" in names
        # Non-task windows are NOT synthesized (they're only included
        # via the StateStore session records path).
        assert "pm-operator" not in names

    def test_successful_notify_clears_sweep_no_session_alert(self, state_store):
        # Pre-existing sweep-level alert from a prior tick where the
        # per-task session wasn't yet live. Once notify resolves and
        # sends, both the per-task ``no_session_for_assignment:<id>``
        # and the role-level ``(worker-<project>, no_session)`` alerts
        # must clear so heartbeat output stops flagging the (now-live)
        # worker.
        state_store.upsert_alert(
            "worker-blackjack-trainer",
            "no_session",
            "warn",
            "stub",
        )
        svc = FakeSessionService(handles=[
            FakeHandle("task-blackjack-trainer-3"),
        ])
        services = _RuntimeServices(
            session_service=svc, state_store=state_store,
            work_service=None, project_root=Path("."),
            msg_store=state_store,
        )
        event = TaskAssignmentEvent(
            task_id="blackjack-trainer/3",
            project="blackjack-trainer",
            task_number=3,
            title="Add charts",
            current_node="implement",
            current_node_kind="work",
            actor_type=ActorType.ROLE,
            actor_name="worker",
            work_status="in_progress",
            priority="normal",
            transitioned_at=datetime.now(timezone.utc),
            transitioned_by="tester",
        )
        outcome = notify(event, services=services)
        assert outcome["outcome"] == "sent"
        open_types = {a.alert_type for a in state_store.open_alerts()}
        assert "no_session" not in open_types


# ---------------------------------------------------------------------------
# #952 — concurrent-sweep dedupe race
# ---------------------------------------------------------------------------


class TestConcurrentSweepDedupeRace:
    """Two concurrent ``notify()`` calls racing on the same fresh task
    must result in EXACTLY ONE ``Resume work`` ping reaching the worker
    pane. The legacy flow (read was_notified_within → send → record)
    left a TOCTOU window where both callers saw "not yet sent" and each
    fired a duplicate before either committed the row. The fix claims
    the dedupe slot atomically before the send, so the second caller
    observes the placeholder and returns ``deduped``. (#952)
    """

    def test_serial_back_to_back_sends_dedupe_to_one_message(self, state_store):
        """Two ``notify()`` calls for the same ``(session, task, version)``
        deliver the message exactly once, even when the second call
        arrives before the first call's ``record_notification`` would
        have landed under the legacy flow. With the atomic-claim fix the
        second call sees the placeholder and dedupes."""
        svc = FakeSessionService(handles=[FakeHandle("worker-demo")])
        services = _RuntimeServices(
            session_service=svc, state_store=state_store,
            work_service=None, project_root=Path("."),
        )

        first = notify(_event(), services=services, throttle_seconds=300)
        second = notify(_event(), services=services, throttle_seconds=300)

        assert first["outcome"] == "sent"
        assert second["outcome"] == "deduped"
        # Worker pane received exactly one ping — the race fix ensures
        # the second caller's claim returned None before the send fired.
        assert len(svc.sent) == 1, (
            f"expected exactly one delivered message, got {svc.sent!r}"
        )

    def test_threaded_concurrent_notify_sends_exactly_once(self, state_store):
        """Drive two threads that both call ``notify()`` for the same
        task simultaneously — modeling two sweep ticks racing on a
        freshly-claimed worker. Exactly one thread wins the dedupe
        claim and the other returns ``deduped``; the worker pane sees
        a single ``Resume work`` payload."""
        import threading

        # Block the send call until both threads have crossed the
        # dedupe-claim boundary, so we're actually testing the claim
        # logic rather than the inherent serialization of the send.
        send_gate = threading.Event()
        sent_lock = threading.Lock()

        @dataclass
        class GatedSession:
            handles: list[FakeHandle]
            sent: list[tuple[str, str]] = field(default_factory=list)

            def list(self) -> list[FakeHandle]:
                return list(self.handles)

            def send(self, name: str, text: str, *, press_enter: bool = True) -> None:
                # Wait briefly so the second thread's claim attempt
                # races against the first thread's pending row.
                send_gate.wait(timeout=2.0)
                with sent_lock:
                    self.sent.append((name, text))

            def is_turn_active(self, name: str) -> bool:
                return False

        svc = GatedSession(handles=[FakeHandle("worker-demo")])
        services = _RuntimeServices(
            session_service=svc, state_store=state_store,
            work_service=None, project_root=Path("."),
        )

        results: list[dict] = []
        results_lock = threading.Lock()

        def _worker():
            outcome = notify(_event(), services=services, throttle_seconds=300)
            with results_lock:
                results.append(outcome)

        threads = [threading.Thread(target=_worker) for _ in range(2)]
        for t in threads:
            t.start()
        # Brief slack so both threads enter notify() before we let the
        # send proceed. The atomic claim runs synchronously under the
        # StateStore lock, so by the time we release the gate one
        # thread has already won the claim and the other has bailed
        # out with ``deduped`` — even if the send happens to be slow.
        import time
        time.sleep(0.05)
        send_gate.set()
        for t in threads:
            t.join(timeout=5.0)

        outcomes = sorted(r["outcome"] for r in results)
        assert outcomes == ["deduped", "sent"], (
            f"expected one sent + one deduped, got {outcomes!r}"
        )
        assert len(svc.sent) == 1, (
            f"expected exactly one delivered message under race, got {svc.sent!r}"
        )

    def test_claim_failure_falls_back_to_send_instead_of_false_dedupe(self):
        """If the atomic claim helper itself errors, ``notify()`` must not
        label the event ``deduped`` and drop the kickoff. It should fall
        back to the legacy serial dedupe path and still send when that
        path reports no recent ping (#952 follow-up)."""

        class BrokenClaimStore:
            def __init__(self) -> None:
                self.records: list[dict] = []

            def claim_notification_slot(self, **kwargs):  # noqa: ANN001
                raise RuntimeError("db temporarily locked")

            def was_notified_within(self, *args, **kwargs):  # noqa: ANN001
                return False

            def record_notification(self, **kwargs):  # noqa: ANN001
                self.records.append(dict(kwargs))

        store = BrokenClaimStore()
        svc = FakeSessionService(handles=[FakeHandle("worker-demo")])
        services = _RuntimeServices(
            session_service=svc,
            state_store=store,
            work_service=None,
            project_root=Path("."),
        )

        outcome = notify(_event(), services=services, throttle_seconds=300)

        assert outcome["outcome"] == "sent"
        assert len(svc.sent) == 1
        assert len(store.records) == 1

    def test_forced_kickoff_dedupes_without_historical_throttle(self, state_store):
        """Forced kickoff uses ``throttle_seconds=0`` to bypass stale
        normal notification rows, but it still needs a short atomic slot
        so repeated/concurrent kickoff-pending sweeps don't stack Resume
        pings during boot (#952 follow-up)."""
        svc = FakeSessionService(handles=[FakeHandle("worker-demo")])
        services = _RuntimeServices(
            session_service=svc,
            state_store=state_store,
            work_service=None,
            project_root=Path("."),
        )

        first = notify(
            _event(work_status="in_progress"),
            services=services,
            throttle_seconds=0,
            atomic_dedupe_seconds=60,
            dedupe_scope="forced_kickoff",
        )
        second = notify(
            _event(work_status="in_progress"),
            services=services,
            throttle_seconds=0,
            atomic_dedupe_seconds=60,
            dedupe_scope="forced_kickoff",
        )
        normal_followup = notify(
            _event(work_status="in_progress"),
            services=services,
            throttle_seconds=300,
        )

        assert first["outcome"] == "sent"
        assert second["outcome"] == "deduped"
        assert normal_followup["outcome"] == "deduped"
        assert len(svc.sent) == 1

    def test_forced_kickoff_ignores_stale_normal_notification(self, state_store):
        """A previous normal notification row should not suppress a forced
        kickoff. That's the poisoned-row case #922 introduced
        ``throttle_seconds=0`` for; the forced-kickoff scope must bypass
        it while still deduping other forced-kickoff rows."""
        state_store.record_notification(
            session_name="worker-demo",
            task_id="demo/1",
            project="demo",
            message="poisoned pre-pane send",
            delivery_status="sent",
            execution_version=0,
        )
        svc = FakeSessionService(handles=[FakeHandle("worker-demo")])
        services = _RuntimeServices(
            session_service=svc,
            state_store=state_store,
            work_service=None,
            project_root=Path("."),
        )

        outcome = notify(
            _event(work_status="in_progress"),
            services=services,
            throttle_seconds=0,
            atomic_dedupe_seconds=60,
            dedupe_scope="forced_kickoff",
        )

        assert outcome["outcome"] == "sent"
        assert len(svc.sent) == 1
