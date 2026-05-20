"""Pg re-coverage for task-assignment notify against PgWorkService (#1824).

PR #1931 (commit a8f8620e9) added
``tests/test_task_assignment_notify_helpers.py`` covering the
backend-neutral pure helpers (role resolution, ping formatting,
StateStore dedupe / escalation). This module extends that coverage
to the surfaces that need the **PgWorkService** harness:

* The sweeper's per-tick fanout via
  :func:`task_assignment_sweep_handler` running against a
  pg-backed work service, including the #921 per-task session
  recognition and the dedupe-cooldown skip.
* End-to-end ``create -> queue -> TaskAssignmentEvent`` emission
  through the bus against ``PgWorkService``.
* Per-task worker recognition + the #1439
  singleton-roles-never-route-to-per-task-pane invariants (these
  belong here because they probe ``SessionRoleIndex`` resolution
  for per-task windows that PR #1931 didn't exercise).

Pure-helper coverage that does *not* need a work-service fixture
(``role_candidate_names`` invariants, ``format_ping_for_role``
strings, ``notify`` dedupe + escalation against a tmp_path
``StateStore``, payload round-trip) lives in
``tests/test_task_assignment_notify_helpers.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pollypm.plugins_builtin.task_assignment_notify.handlers.sweep import (
    task_assignment_sweep_handler,
)
from pollypm.plugins_builtin.task_assignment_notify.resolver import (
    _RuntimeServices,
)
from pollypm.storage.state import StateStore
from pollypm.work import task_assignment as bus
from pollypm.work.models import ActorType
from pollypm.work.task_assignment import (
    SessionRoleIndex,
    TaskAssignmentEvent,
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
    actor_type: ActorType = ActorType.ROLE,
    actor_name: str = "worker",
    current_node: str = "do_work",
    current_node_kind: str = "work",
    work_status: str = "queued",
) -> TaskAssignmentEvent:
    return TaskAssignmentEvent(
        task_id=task_id,
        project=project,
        task_number=int(task_id.split("/", 1)[1]),
        title="Build the thing",
        current_node=current_node,
        current_node_kind=current_node_kind,
        actor_type=actor_type,
        actor_name=actor_name,
        work_status=work_status,
        priority="normal",
        transitioned_at=datetime.now(timezone.utc),
        transitioned_by="tester",
    )


@pytest.fixture
def state_store(tmp_path):
    """Per-test StateStore (still sqlite, #342-followup)."""
    store = StateStore(tmp_path / "state.db")
    yield store
    store.close()


# ---------------------------------------------------------------------------
# SessionRoleIndex — multiple-matches selection.
# ---------------------------------------------------------------------------


def test_multiple_matches_prefers_least_busy():
    """When both dash and underscore candidates exist, the resolver picks
    the one with fewer in-progress claims."""

    class FakeWork:
        def list_tasks(self, *, work_status=None, assignee=None, **kw):
            if assignee == "worker-demo":
                return [
                    type("T", (), {
                        "work_status": type("S", (), {"value": "in_progress"})(),
                        "assignee": "worker-demo",
                    })()
                    for _ in range(3)
                ]
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
# Reviewer-routing dedicated test against StateStore alert dispatch (#1439).
# ---------------------------------------------------------------------------


class TestReviewerNotifyRouting:
    def test_review_ping_targets_reviewer_not_task_worker(self, state_store):
        """#1439 — review pings must land on the long-lived reviewer
        lane even when the per-task worker window for that task is
        still alive."""
        from pollypm.plugins_builtin.task_assignment_notify.resolver import (
            notify,
        )

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


# ---------------------------------------------------------------------------
# Sweeper — pg work service driven through fake session service.
# ---------------------------------------------------------------------------


class TestSweeperPg:
    def _services(self, pg_work_service, state_store, session_handles):
        return _RuntimeServices(
            session_service=FakeSessionService(handles=list(session_handles)),
            state_store=state_store,
            work_service=pg_work_service,
            project_root=Path("."),
        )

    def test_sweeper_picks_up_preexisting_queued_task(
        self, pg_work_service, state_store, monkeypatch,
    ):
        bus.clear_listeners()
        services = self._services(
            pg_work_service, state_store, [FakeHandle("worker-proj")],
        )
        task = pg_work_service.create(
            title="Preexisting work",
            description="Make stuff",
            type="task",
            project="proj",
            flow_template="standard",
            roles={"worker": "agent-1", "reviewer": "agent-2"},
            priority="normal",
        )
        pg_work_service.queue(task.task_id, "pm")

        monkeypatch.setattr(
            "pollypm.plugins_builtin.task_assignment_notify.handlers.sweep.load_runtime_services",
            lambda *, config_path=None: services,
        )

        result = task_assignment_sweep_handler({})
        assert result["outcome"] == "swept"
        assert result["considered"] >= 1
        assert result["by_outcome"].get("sent", 0) >= 1
        assert any(
            "New work" in text for _name, text in services.session_service.sent
        )
        bus.clear_listeners()

    def test_sweeper_recognises_per_task_session_for_in_progress(
        self, pg_work_service, state_store, monkeypatch,
    ):
        """#921 — in_progress task with a live ``task-<proj>-<N>``
        session must NOT raise ``no_session``."""
        bus.clear_listeners()
        task = pg_work_service.create(
            title="Add charts",
            description="Implement",
            type="task",
            project="blackjack-trainer",
            flow_template="standard",
            roles={"worker": "agent-1", "reviewer": "agent-2"},
            priority="normal",
        )
        pg_work_service.queue(task.task_id, "pm")
        pg_work_service.claim(task.task_id, "worker")
        live_window = f"task-{task.project}-{task.task_number}"
        services = _RuntimeServices(
            session_service=FakeSessionService(handles=[FakeHandle(live_window)]),
            state_store=state_store,
            work_service=pg_work_service,
            project_root=Path("."),
            msg_store=state_store,
        )

        monkeypatch.setattr(
            "pollypm.plugins_builtin.task_assignment_notify.handlers.sweep.load_runtime_services",
            lambda *, config_path=None: services,
        )

        result = task_assignment_sweep_handler({})
        assert result["outcome"] == "swept"
        assert result["by_outcome"].get("no_session", 0) == 0
        assert any(
            name == live_window for name, _t in services.session_service.sent
        )
        assert [
            a for a in state_store.open_alerts()
            if a.alert_type == "no_session"
        ] == []
        assert [
            a for a in state_store.open_alerts()
            if a.alert_type.startswith("no_session_for_assignment:")
        ] == []
        bus.clear_listeners()

    def test_sweeper_skips_already_notified_within_cooldown(
        self, pg_work_service, state_store, monkeypatch,
    ):
        bus.clear_listeners()
        services = self._services(
            pg_work_service, state_store, [FakeHandle("worker-proj")],
        )
        task = pg_work_service.create(
            title="Preexisting work",
            description="Desc",
            type="task",
            project="proj",
            flow_template="standard",
            roles={"worker": "agent-1", "reviewer": "agent-2"},
            priority="normal",
        )
        pg_work_service.queue(task.task_id, "pm")

        state_store.record_notification(
            session_name="worker-proj",
            task_id=task.task_id,
            project="proj",
            message="stub",
            delivery_status="sent",
        )

        monkeypatch.setattr(
            "pollypm.plugins_builtin.task_assignment_notify.handlers.sweep.load_runtime_services",
            lambda *, config_path=None: services,
        )

        result = task_assignment_sweep_handler({})
        assert result["by_outcome"].get("deduped", 0) >= 1
        assert len(services.session_service.sent) == 0
        bus.clear_listeners()


# ---------------------------------------------------------------------------
# End-to-end: task transition emits TaskAssignmentEvent through bus.
# ---------------------------------------------------------------------------


class TestEndToEndTransitionPg:
    def test_queue_fires_event(self, pg_work_service):
        bus.clear_listeners()
        events: list[TaskAssignmentEvent] = []
        bus.register_listener(events.append)
        try:
            task = pg_work_service.create(
                title="Ship it",
                description="Implement feature X",
                type="task",
                project="proj",
                flow_template="standard",
                roles={"worker": "agent-1", "reviewer": "agent-2"},
                priority="normal",
            )
            pg_work_service.queue(task.task_id, "pm")

            assert events, "Queue transition should emit a TaskAssignmentEvent"
            event = events[-1]
            assert event.task_id == task.task_id
            assert event.project == "proj"
            assert event.actor_type is ActorType.ROLE
            assert event.actor_name == "worker"
            assert event.work_status == "queued"
            assert event.current_node_kind == "work"
        finally:
            bus.clear_listeners()

    def test_human_node_does_not_emit(self, pg_work_service):
        """A transition into a HUMAN review node should not ping a session."""
        bus.clear_listeners()
        events: list[TaskAssignmentEvent] = []
        bus.register_listener(events.append)
        try:
            task = pg_work_service.create(
                title="With human review",
                description="Needs a human signoff",
                type="task",
                project="proj",
                flow_template="standard",
                roles={"worker": "agent-1", "reviewer": "agent-2"},
                priority="normal",
                requires_human_review=True,
            )
            pg_work_service.queue(task.task_id, "pm", skip_gates=True)
            events.clear()

            pg_work_service.claim(task.task_id, "agent-1")
            from pollypm.work.models import (
                Artifact,
                ArtifactKind,
                OutputType,
                WorkOutput,
            )

            out = WorkOutput(
                type=OutputType.CODE_CHANGE,
                summary="Implemented feature X",
                artifacts=[Artifact(
                    kind=ArtifactKind.COMMIT,
                    description="feat: X",
                    ref="abc123",
                )],
            )
            pg_work_service.node_done(
                task.task_id, "agent-1", work_output=out, skip_gates=True,
            )
            assert all(e.actor_type is not ActorType.HUMAN for e in events)
        finally:
            bus.clear_listeners()


# ---------------------------------------------------------------------------
# Per-task worker recognition — pure resolver behaviour.
# ---------------------------------------------------------------------------


class TestPerTaskWorkerRecognition:
    """#919 — when the caller knows the task number, the per-task window
    candidate comes first so a freshly-spawned per-task worker pane
    gets the kickoff ping."""

    def test_role_candidates_prepend_per_task_window(self):
        cands = role_candidate_names("worker", "blackjack", task_number=12)
        assert cands[0] == "task-blackjack-12"
        assert "worker-blackjack" in cands

    def test_role_candidates_no_task_number_unchanged(self):
        cands = role_candidate_names("worker", "demo")
        assert "task-demo-1" not in cands
        assert cands == ["worker-demo", "worker_demo"]

    def test_resolver_picks_task_window_over_legacy_worker(self):
        svc = FakeSessionService(handles=[
            FakeHandle("task-demo-7"),
            FakeHandle("worker-demo"),
        ])
        index = SessionRoleIndex(svc)
        handle = index.resolve(
            ActorType.ROLE, "worker", "demo", task_number=7,
        )
        assert handle is not None
        assert handle.name == "task-demo-7"

    def test_resolver_falls_back_to_legacy_worker_when_no_task_window(self):
        svc = FakeSessionService(handles=[FakeHandle("worker-demo")])
        index = SessionRoleIndex(svc)
        handle = index.resolve(
            ActorType.ROLE, "worker", "demo", task_number=7,
        )
        assert handle is not None
        assert handle.name == "worker-demo"

    def test_resolver_does_not_match_sibling_task_window(self):
        """A per-task window for a different task number must not be
        picked when resolving a different task's worker."""
        svc = FakeSessionService(handles=[FakeHandle("task-demo-7")])
        index = SessionRoleIndex(svc)
        handle = index.resolve(
            ActorType.ROLE, "worker", "demo", task_number=8,
        )
        assert handle is None


class TestSingletonRolesNeverRouteToPerTaskPane:
    """#1439 — singleton-only roles (reviewer / operator / triage /
    heartbeat) must NEVER route to per-task panes, even when the
    per-task pane's project matches.
    """

    def test_reviewer_kickoff_does_not_route_to_per_task_worker(self):
        svc = FakeSessionService(handles=[
            FakeHandle("task-demo-7"),
            FakeHandle("reviewer_demo"),
        ])
        index = SessionRoleIndex(svc)
        handle = index.resolve(
            ActorType.ROLE, "reviewer", "demo", task_number=7,
        )
        assert handle is not None
        assert handle.name == "reviewer_demo"

    def test_reviewer_kickoff_returns_none_when_only_per_task_pane_alive(self):
        svc = FakeSessionService(handles=[FakeHandle("task-demo-7")])
        index = SessionRoleIndex(svc)
        assert index.resolve(
            ActorType.ROLE, "reviewer", "demo", task_number=7,
        ) is None

    def test_operator_kickoff_does_not_route_to_per_task_worker(self):
        svc = FakeSessionService(handles=[
            FakeHandle("task-demo-7"),
            FakeHandle("operator_demo"),
        ])
        index = SessionRoleIndex(svc)
        handle = index.resolve(
            ActorType.ROLE, "operator", "demo", task_number=7,
        )
        assert handle is not None
        assert handle.name == "operator_demo"

    def test_triage_kickoff_does_not_route_to_per_task_worker(self):
        svc = FakeSessionService(handles=[
            FakeHandle("task-demo-7"),
            FakeHandle("triage_demo"),
        ])
        index = SessionRoleIndex(svc)
        handle = index.resolve(
            ActorType.ROLE, "triage", "demo", task_number=7,
        )
        assert handle is not None
        assert handle.name == "triage_demo"

    def test_heartbeat_kickoff_does_not_route_to_per_task_worker(self):
        svc = FakeSessionService(handles=[
            FakeHandle("task-demo-7"),
            FakeHandle("pm-heartbeat"),
        ])
        index = SessionRoleIndex(svc)
        handle = index.resolve(
            ActorType.ROLE, "heartbeat", "demo", task_number=7,
        )
        assert handle is not None
        assert handle.name == "pm-heartbeat"

    def test_role_candidates_for_reviewer_omits_per_task_pane(self):
        cands = role_candidate_names("reviewer", "demo", task_number=7)
        assert "task-demo-7" not in cands

    def test_role_candidates_for_operator_omits_per_task_pane(self):
        cands = role_candidate_names("operator", "demo", task_number=7)
        assert "task-demo-7" not in cands

    def test_role_candidates_for_triage_omits_per_task_pane(self):
        cands = role_candidate_names("triage", "demo", task_number=7)
        assert "task-demo-7" not in cands

    def test_role_candidates_for_heartbeat_omits_per_task_pane(self):
        cands = role_candidate_names("heartbeat", "demo", task_number=7)
        assert "task-demo-7" not in cands

    def test_worker_role_still_resolves_to_per_task_pane(self):
        svc = FakeSessionService(handles=[
            FakeHandle("task-demo-7"),
            FakeHandle("worker-demo"),
        ])
        index = SessionRoleIndex(svc)
        handle = index.resolve(
            ActorType.ROLE, "worker", "demo", task_number=7,
        )
        assert handle is not None
        assert handle.name == "task-demo-7"
