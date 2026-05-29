"""Re-coverage of task-assignment-notify helpers (refs #1824).

Ports the backend-agnostic half of the deleted
``tests/test_task_assignment_notify.py`` (2,518 LOC, removed by Slice
K-tests part 5/6 — see #1737). The slice that landed deleted everything
on the grounds that ``SQLiteWorkService`` was going away; the helpers
themselves (role resolution, ping formatting, payload round-trip,
``build_event_from_task``) are pure functions that have nothing to do
with the storage backend, so re-adding their coverage costs nothing
and protects a surface the PG cutover did not exercise.

Scope picked here:

* ``role_candidate_names`` — naming convention enumeration (#1011 /
  #1439 per-project + reviewer lane semantics).
* ``SessionRoleIndex.resolve`` — session lookup against a fake
  ``SessionService`` listing.
* ``format_ping_for_role`` — operator-facing ping copy.
* ``event_to_payload`` — JSON round-trip for ``JobQueue.enqueue``.
* ``build_event_from_task`` — HUMAN-node carve-out.
* ``notify``-side dedupe + escalation — exercised against a
  ``tmp_path`` ``StateStore`` (state.py is still sqlite — #342-followup
  tracks the pg port; the helper does not care).

Out of scope here (belongs in a later batch):

* End-to-end transition tests that re-create a ``SQLiteWorkService``;
  ``test_pg_task_assignment_fanout.py`` in this PR covers the pg
  equivalent.
* The sweeper's per-task / per-project fan-out paths — covered by the
  fanout port in this PR.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from pollypm.plugins_builtin.task_assignment_notify.handlers.notify import (
    event_to_payload,
)
from pollypm.plugins_builtin.task_assignment_notify.resolver import (
    _RuntimeServices,
    notify,
)
from pollypm.storage.state import StateStore
from pollypm.work.models import ActorType, FlowNode, NodeType
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


@pytest.fixture
def state_store(tmp_path):
    store = StateStore(tmp_path / "state.db")
    yield store
    store.close()


# ---------------------------------------------------------------------------
# Role candidate enumeration
# ---------------------------------------------------------------------------


class TestRoleCandidates:
    def test_worker_expands_to_both_separators(self):
        assert role_candidate_names("worker", "demo") == [
            "worker-demo",
            "worker_demo",
        ]

    def test_reviewer_with_project_prepends_per_project(self):
        # #1011 — when a project is supplied the per-project candidates
        # come first so the resolver sees a ``reviewer_<project>`` session
        # spawned by ``pm worker-start --role reviewer <project>``.
        # Singleton form stays as the fallback (#272).
        assert role_candidate_names("reviewer", "bikepath") == [
            "reviewer_bikepath",
            "reviewer-bikepath",
            "reviewer",
            "pm-reviewer",
        ]

    def test_advisor_is_project_scoped(self):
        assert role_candidate_names("advisor", "bikepath") == [
            "advisor-bikepath",
            "advisor_bikepath",
        ]

    def test_reviewer_with_task_number_still_uses_reviewer_lane(self):
        # #1439 — a still-open per-task worker pane must not steal
        # review-node handoff pings from the long-lived reviewer lane.
        assert role_candidate_names(
            "reviewer", "savethenovel", task_number=12,
        ) == [
            "reviewer_savethenovel",
            "reviewer-savethenovel",
            "reviewer",
            "pm-reviewer",
        ]

    def test_reviewer_without_project_pins_to_singleton(self):
        # Empty project key → singleton-only (legacy behaviour).
        assert role_candidate_names("reviewer", "") == [
            "reviewer",
            "pm-reviewer",
        ]

    def test_operator_with_project_prepends_per_project(self):
        # #1011 — same as reviewer; singleton form is the fallback.
        assert role_candidate_names("operator", "x") == [
            "operator_x",
            "operator-x",
            "operator",
            "pm-operator",
        ]

    def test_heartbeat_supervisor_pins_to_pm_heartbeat(self):
        # #1011 — heartbeat is the per-workspace supervisor; per-project
        # candidates are still emitted for symmetry but in practice
        # ``no_session`` never opens for heartbeat (it's bootstrapped by
        # the supervisor, not the auto-recovery sweep).
        assert role_candidate_names("heartbeat-supervisor", "x") == [
            "heartbeat-supervisor_x",
            "heartbeat-supervisor-x",
            "heartbeat",
            "pm-heartbeat",
        ]
        assert role_candidate_names("heartbeat", "x") == [
            "heartbeat_x",
            "heartbeat-x",
            "heartbeat",
            "pm-heartbeat",
        ]

    def test_triage_with_project_prepends_per_project(self):
        # #1011.
        assert role_candidate_names("triage", "x") == [
            "triage_x",
            "triage-x",
            "triage",
            "pm-triage",
        ]

    def test_critic_passes_through(self):
        assert role_candidate_names("critic_simplicity", "x") == [
            "critic_simplicity",
        ]

    def test_unknown_role_yields_no_candidates(self):
        assert role_candidate_names("invented", "x") == []


# ---------------------------------------------------------------------------
# Session role index resolve
# ---------------------------------------------------------------------------


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
        # #1439 — the resolver must not return a per-task worker pane
        # when looking up the reviewer for the same task.
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
            FakeHandle("polly"),
            FakeHandle("pm-reviewer"),
        ])
        index = SessionRoleIndex(svc)
        handle = index.resolve(ActorType.AGENT, "polly", "demo")
        assert handle is not None
        assert handle.name == "polly"

    def test_human_returns_none(self):
        svc = FakeSessionService(handles=[FakeHandle("pm-reviewer")])
        index = SessionRoleIndex(svc)
        assert (
            index.resolve(ActorType.HUMAN, "reviewer", "demo") is None
        )

    def test_no_matching_session_returns_none(self):
        svc = FakeSessionService(handles=[FakeHandle("worker-other")])
        index = SessionRoleIndex(svc)
        assert index.resolve(ActorType.ROLE, "worker", "demo") is None


# ---------------------------------------------------------------------------
# Ping message formatting
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
# Notify dedupe + escalation against tmp_path StateStore
# ---------------------------------------------------------------------------


class TestNotifyDedupe:
    def test_first_notification_sends(self, state_store):
        svc = FakeSessionService(handles=[FakeHandle("worker-demo")])
        services = _RuntimeServices(
            session_service=svc,
            state_store=state_store,
            work_service=None,
            project_root=Path("."),
        )
        outcome = notify(_event(), services=services)
        assert outcome["outcome"] == "sent"
        assert len(svc.sent) == 1
        assert svc.sent[0][0] == "worker-demo"

    def test_second_notification_within_window_deduped(self, state_store):
        svc = FakeSessionService(handles=[FakeHandle("worker-demo")])
        services = _RuntimeServices(
            session_service=svc,
            state_store=state_store,
            work_service=None,
            project_root=Path("."),
        )
        notify(_event(), services=services)
        outcome = notify(_event(), services=services)
        assert outcome["outcome"] == "deduped"
        assert len(svc.sent) == 1  # still just the first

    def test_past_throttle_resends(self, state_store):
        svc = FakeSessionService(handles=[FakeHandle("worker-demo")])
        services = _RuntimeServices(
            session_service=svc,
            state_store=state_store,
            work_service=None,
            project_root=Path("."),
        )
        notify(_event(), services=services)
        outcome = notify(
            _event(), services=services, throttle_seconds=0,
        )
        assert outcome["outcome"] == "sent"
        assert len(svc.sent) == 2

    def test_paused_session_skips_assignment_send(self, tmp_path, state_store):
        from pollypm.session_paused import _reset_skip_throttle_for_tests

        _reset_skip_throttle_for_tests()
        base_dir = tmp_path / ".pollypm"
        base_dir.mkdir()
        (base_dir / "paused-sessions.json").write_text('["worker-demo"]\n')

        config = SimpleNamespace(
            project=SimpleNamespace(
                name="demo",
                root_dir=tmp_path,
                base_dir=base_dir,
            )
        )

        svc = FakeSessionService(handles=[FakeHandle("worker-demo")])
        services = _RuntimeServices(
            session_service=svc,
            state_store=state_store,
            work_service=None,
            project_root=Path("."),
            config=config,
        )

        outcome = notify(_event(), services=services)

        assert outcome["outcome"] == "skipped_paused"
        assert outcome["session"] == "worker-demo"
        assert svc.sent == []
        events = state_store.recent_events(5)
        assert any(
            event.event_type == "session.pause.skip"
            and event.session_name == "worker-demo"
            for event in events
        )


class TestNotifyEscalation:
    def test_no_matching_session_raises_alert(self, state_store):
        svc = FakeSessionService(handles=[])  # nobody live
        services = _RuntimeServices(
            session_service=svc,
            state_store=state_store,
            work_service=None,
            project_root=Path("."),
        )
        outcome = notify(_event(), services=services)
        assert outcome["outcome"] == "no_session"
        alerts = state_store.open_alerts()
        per_task = [
            a for a in alerts
            if a.alert_type == "no_session_for_assignment:demo/1"
        ]
        assert per_task, "expected per-task no_session alert"
        # Worker-role no-session alert points at ``pm task claim`` as the
        # per-task recovery path (#953). It must not suggest ``pm task
        # approve`` — that is reviewer-only.
        msg = per_task[0].message
        assert "Open the task in Tasks" in msg
        assert "Try: pm task claim demo/1" in msg
        assert "pm task approve" not in msg

    def test_reviewer_no_session_hint_points_to_review_ui(self, state_store):
        # #953 — reviewer-role no-session alerts must surface human
        # Approve/Reject as the canonical path AND lead the ``Try:`` block
        # with ``pm task approve`` for CLI-only operators.
        svc = FakeSessionService(handles=[])
        services = _RuntimeServices(
            session_service=svc,
            state_store=state_store,
            work_service=None,
            project_root=Path("."),
        )
        outcome = notify(
            _event(
                actor_name="reviewer",
                current_node="review",
                current_node_kind="review",
            ),
            services=services,
        )
        assert outcome["outcome"] == "no_session"
        alerts = state_store.open_alerts()
        matching = [
            a for a in alerts if a.alert_type.endswith(":demo/1")
        ]
        assert matching, "expected per-task no_session_for_assignment alert"
        message = matching[0].message
        assert "Open the task in Tasks or Inbox" in message
        assert "Approve or Reject" in message
        assert "Try: pm task approve demo/1" in message
        # ``pm task approve`` listed FIRST in the Try: block.
        approve_idx = message.find("pm task approve demo/1")
        worker_start_idx = message.find(
            "pm worker-start --role reviewer demo",
        )
        claim_idx = message.find("pm task claim demo/1")
        assert approve_idx != -1
        if worker_start_idx != -1:
            assert approve_idx < worker_start_idx
        if claim_idx != -1:
            assert approve_idx < claim_idx


# ---------------------------------------------------------------------------
# Payload round-trip + HUMAN-node carve-out
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


def test_build_event_from_task_returns_none_for_human_node():
    """Direct coverage on the helper that decides whether to emit."""

    class _Task:
        task_id = "demo/1"
        project = "proj"
        task_number = 1
        title = "t"
        current_node_id = "review_node"
        priority = None
        work_status = None

    human_node = FlowNode(
        name="review",
        type=NodeType.REVIEW,
        actor_type=ActorType.HUMAN,
        actor_role="reviewer",
    )
    assert build_event_from_task(
        _Task(), human_node, transitioned_by="tester",
    ) is None


# ---------------------------------------------------------------------------
# StateStore schema invariant — ensures the notification table exists
# and round-trips correctly. StateStore is still sqlite today (#342-
# followup); this test guards the contract the resolver depends on
# regardless of which backend ultimately ships behind ``StateStore``.
# ---------------------------------------------------------------------------


def test_state_store_records_and_dedupes_notifications(tmp_path):
    store = StateStore(tmp_path / "state.db")
    try:
        store.record_notification(
            session_name="worker-demo",
            task_id="demo/1",
            project="demo",
            message="hi",
            delivery_status="sent",
        )
        assert store.was_notified_within("worker-demo", "demo/1", 60)
        rows = store.recent_notifications(limit=10)
        assert rows
        assert rows[0]["session_name"] == "worker-demo"
    finally:
        store.close()
