"""Tests for #296 — observable flow-state drift detection.

Three layers of coverage:

* ``reconcile_expected_advance`` as a pure function (no work-service
  required for the plan-heuristic path) — the core decision table.
* ``DefaultRecoveryPolicy.classify`` — the ``state_drift`` signal
  path in/out of the intervention ladder.
* ``work_progress_sweep_handler`` integration — drift detection
  alongside the existing ``stuck_on_task`` path, including event +
  alert emission and dedupe across sweeps.

The plan-project scenario is the real dogfood motivator: Archie wrote
the plan and fired a ``pm notify`` but his task node stayed on
``research``. State said one thing, deliverables said another.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from pollypm.plugins_builtin.core_recurring.plugin import (
    work_progress_sweep_handler,
)
from pollypm.plugins_builtin.task_assignment_notify.resolver import (
    _RuntimeServices,
)
from pollypm.recovery.base import SessionHealth, SessionSignals
from pollypm.recovery.default import DefaultRecoveryPolicy
from pollypm.recovery.state_reconciliation import (
    MIN_PLAN_SIZE_BYTES,
    ReconciliationAction,
    reconcile_expected_advance,
)
from pollypm.storage.state import StateStore
from pollypm.work import task_assignment as bus
from pollypm.work.sqlite_service import SQLiteWorkService


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeHandle:
    name: str


@dataclass
class FakeSessionService:
    handles: list[FakeHandle]
    sent: list[tuple[str, str]] = field(default_factory=list)
    busy: set[str] = field(default_factory=set)

    def list(self) -> list[FakeHandle]:
        return list(self.handles)

    def send(self, name: str, text: str, *, press_enter: bool = True) -> None:
        self.sent.append((name, text))

    def is_turn_active(self, name: str) -> bool:
        return name in self.busy


# ---------------------------------------------------------------------------
# Task doubles — the reconciliation helper only reads attributes.
# ---------------------------------------------------------------------------


@dataclass
class FakeTask:
    project: str
    task_number: int
    flow_template_id: str
    current_node_id: str
    title: str = "Plan the project"

    @property
    def task_id(self) -> str:
        return f"{self.project}/{self.task_number}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_plan(path: Path, size_bytes: int = MIN_PLAN_SIZE_BYTES + 200) -> Path:
    """Write a plan.md of the requested size at the given path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    filler = "The plan. " * ((size_bytes // 10) + 1)
    path.write_text(filler, encoding="utf-8")
    return path


def _record_plan_ready_notify(
    store: StateStore, project: str, actor: str = "architect",
) -> None:
    """Record a ``pm notify``-shaped event so the reconciler sees it."""
    store.record_event(
        actor,
        "inbox.message.created",
        (
            f"{actor} -> user: Plan ready for approval on {project} — "
            f"please review docs/plan/plan.md"
        ),
    )


def _claim_plan_task(work: SQLiteWorkService, project: str) -> str:
    """Create + queue + claim a plan_project task. Returns its id."""
    task = work.create(
        title="Plan the project",
        description="Produce the project plan.",
        type="task",
        project=project,
        flow_template="plan_project",
        roles={"architect": "architect"},
        priority="normal",
    )
    work.queue(task.task_id, "pm")
    work.claim(task.task_id, "architect")
    return task.task_id


# ---------------------------------------------------------------------------
# 1. Pure reconciliation function
# ---------------------------------------------------------------------------


class TestReconcileExpectedAdvance:
    """Pure-function tests. No work-service required for plan_project
    heuristics — they're short-circuited on the flow id."""

    def test_no_deliverables_returns_none(self, tmp_path: Path) -> None:
        task = FakeTask(
            project="demo", task_number=1,
            flow_template_id="plan_project",
            current_node_id="research",
        )
        result = reconcile_expected_advance(
            task, tmp_path, work_service=None, state_store=None,
        )
        assert result is None

    def test_plan_and_notify_routes_to_user_approval(
        self, tmp_path: Path,
    ) -> None:
        _write_plan(tmp_path / "docs" / "plan" / "plan.md")
        store = StateStore(tmp_path / "state.db")
        _record_plan_ready_notify(store, project="demo")

        task = FakeTask(
            project="demo", task_number=1,
            flow_template_id="plan_project",
            current_node_id="research",
        )
        result = reconcile_expected_advance(
            task, tmp_path, work_service=None, state_store=store,
        )
        assert result is not None
        assert result.advance_to_node == "user_approval"
        assert "plan" in result.reason.lower()

    def test_plan_file_only_routes_to_synthesize(
        self, tmp_path: Path,
    ) -> None:
        _write_plan(tmp_path / "docs" / "plan" / "plan.md")
        store = StateStore(tmp_path / "state.db")
        # No notify fired.

        task = FakeTask(
            project="demo", task_number=1,
            flow_template_id="plan_project",
            current_node_id="research",
        )
        result = reconcile_expected_advance(
            task, tmp_path, work_service=None, state_store=store,
        )
        assert result is not None
        assert result.advance_to_node == "synthesize"

    def test_legacy_project_plan_path_accepted(
        self, tmp_path: Path,
    ) -> None:
        """Tasks that wrote to docs/project-plan.md (pre-spec-revision
        architects) are honoured too."""
        _write_plan(tmp_path / "docs" / "project-plan.md")
        store = StateStore(tmp_path / "state.db")
        _record_plan_ready_notify(store, project="demo")

        task = FakeTask(
            project="demo", task_number=1,
            flow_template_id="plan_project",
            current_node_id="research",
        )
        result = reconcile_expected_advance(
            task, tmp_path, work_service=None, state_store=store,
        )
        assert result is not None
        assert result.advance_to_node == "user_approval"

    def test_small_plan_is_ignored(self, tmp_path: Path) -> None:
        """A plan.md under the 500-byte threshold is scaffolding, not a plan."""
        plan = tmp_path / "docs" / "plan" / "plan.md"
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text("# Plan\nTODO", encoding="utf-8")
        store = StateStore(tmp_path / "state.db")
        _record_plan_ready_notify(store, project="demo")

        task = FakeTask(
            project="demo", task_number=1,
            flow_template_id="plan_project",
            current_node_id="research",
        )
        result = reconcile_expected_advance(
            task, tmp_path, work_service=None, state_store=store,
        )
        assert result is None

    def test_non_plan_flow_returns_none(self, tmp_path: Path) -> None:
        """A standard worker-flow task with a stray plan.md on disk
        should NOT be treated as drift — heuristic is flow-specific."""
        _write_plan(tmp_path / "docs" / "plan" / "plan.md")
        store = StateStore(tmp_path / "state.db")
        _record_plan_ready_notify(store, project="demo")

        task = FakeTask(
            project="demo", task_number=1,
            flow_template_id="standard",
            current_node_id="do_work",
        )
        result = reconcile_expected_advance(
            task, tmp_path, work_service=None, state_store=store,
        )
        assert result is None

    def test_task_already_at_user_approval_returns_none(
        self, tmp_path: Path,
    ) -> None:
        """When the task has already advanced past synthesize, drift
        detection must not fire — we only reconcile upstream nodes."""
        _write_plan(tmp_path / "docs" / "plan" / "plan.md")
        store = StateStore(tmp_path / "state.db")
        _record_plan_ready_notify(store, project="demo")

        task = FakeTask(
            project="demo", task_number=1,
            flow_template_id="plan_project",
            current_node_id="user_approval",
        )
        result = reconcile_expected_advance(
            task, tmp_path, work_service=None, state_store=store,
        )
        assert result is None


# ---------------------------------------------------------------------------
# 2. Classifier behaviour
# ---------------------------------------------------------------------------


class TestClassifierStateDrift:
    """``DefaultRecoveryPolicy.classify`` promotes sessions with a
    precomputed ``drift_action`` to STATE_DRIFT — but only when the
    session is not turning and holds an active claim."""

    def test_drift_action_routes_to_state_drift(self) -> None:
        policy = DefaultRecoveryPolicy()
        action = ReconciliationAction(
            advance_to_node="user_approval", reason="plan present",
        )
        signals = SessionSignals(
            session_name="architect-demo",
            active_claim_task_id="demo/1",
            turn_active=False,
            drift_action=action,
        )
        assert policy.classify(signals) == SessionHealth.STATE_DRIFT

    def test_turn_active_suppresses_drift(self) -> None:
        """A live turn must never be classified as drift — the agent
        is still working, the synthesize → advance may yet happen."""
        policy = DefaultRecoveryPolicy()
        action = ReconciliationAction(
            advance_to_node="user_approval", reason="plan present",
        )
        signals = SessionSignals(
            session_name="architect-demo",
            active_claim_task_id="demo/1",
            turn_active=True,
            has_transcript_delta=True,
            drift_action=action,
        )
        # turn_active=True + transcript delta → ACTIVE, not STATE_DRIFT.
        assert policy.classify(signals) != SessionHealth.STATE_DRIFT

    def test_no_claim_suppresses_drift(self) -> None:
        """A session with no active claim can't have drift — nothing
        to reconcile from."""
        policy = DefaultRecoveryPolicy()
        action = ReconciliationAction(
            advance_to_node="user_approval", reason="plan present",
        )
        signals = SessionSignals(
            session_name="architect-demo",
            active_claim_task_id=None,
            turn_active=False,
            drift_action=action,
        )
        assert policy.classify(signals) != SessionHealth.STATE_DRIFT

    def test_select_intervention_logs_alert_only(self) -> None:
        """V1 policy: the intervention action is ``reconcile_flow_state``
        and carries the target node in details. No auto-advance."""
        policy = DefaultRecoveryPolicy()
        action = ReconciliationAction(
            advance_to_node="user_approval", reason="plan observed",
        )
        signals = SessionSignals(
            session_name="architect-demo",
            active_claim_task_id="demo/1",
            turn_active=False,
            drift_action=action,
        )
        result = policy.select_intervention(
            SessionHealth.STATE_DRIFT, signals, [],
        )
        assert result is not None
        assert result.action == "reconcile_flow_state"
        assert result.details["advance_to_node"] == "user_approval"
        assert result.details["task_id"] == "demo/1"


# ---------------------------------------------------------------------------
# 3. Integration with ``work.progress_sweep``
# ---------------------------------------------------------------------------


class TestSweepDriftIntegration:
    """The 5-min sweeper detects drift, records a state_drift event, and
    raises a warn-level alert keyed to ``state_drift:<task_id>``.
    Dedupe comes from ``upsert_alert``'s per-type uniqueness guard."""

    def _patch_resolver_with_factory(
        self, monkeypatch, tmp_path, svc, store, msg_store=None,
    ):
        def _fake_loader(*, config_path=None):
            work = SQLiteWorkService(
                db_path=tmp_path / "work.db",
                project_path=tmp_path,
            )
            return _RuntimeServices(
                session_service=svc,
                state_store=store,
                work_service=work,
                project_root=tmp_path,
                msg_store=msg_store,
            )

        monkeypatch.setattr(
            "pollypm.plugins_builtin.task_assignment_notify.resolver.load_runtime_services",
            _fake_loader,
        )

    def test_drift_detected_emits_event_and_alert(
        self, tmp_path: Path, monkeypatch,
    ):
        bus.clear_listeners()
        # Seed a plan_project task claimed by architect-demo.
        seed = SQLiteWorkService(
            db_path=tmp_path / "work.db",
            project_path=tmp_path,
        )
        task_id = _claim_plan_task(seed, "demo")
        seed.close()

        # Write the deliverables + fire the notify.
        _write_plan(tmp_path / "docs" / "plan" / "plan.md")
        store = StateStore(tmp_path / "state.db")
        # #349: drift sweep writes events + alerts via the unified Store.
        from pollypm.store import SQLAlchemyStore
        msg_store = SQLAlchemyStore(f"sqlite:///{tmp_path / 'state.db'}")
        _record_plan_ready_notify(store, project="demo")

        svc = FakeSessionService(handles=[FakeHandle("architect-demo")])
        self._patch_resolver_with_factory(
            monkeypatch, tmp_path, svc, store, msg_store=msg_store,
        )

        result = work_progress_sweep_handler({})
        assert result["outcome"] == "swept"
        assert result["drift_detected"] >= 1, (
            f"expected drift_detected>=1, got {result!r}"
        )
        assert result["drift_alerted"] == 1

        # The state_drift event landed on the architect session.
        events = [
            e for e in msg_store.query_messages(
                type="event", scope="architect-demo", limit=50,
            )
            if e.get("subject") == "state_drift"
        ]
        assert len(events) == 1
        assert events[0]["scope"] == "architect-demo"
        # The event message narrates the node transition so an operator
        # can see the drift without chasing the alert.
        message = (events[0].get("payload") or {}).get("message") or ""
        assert task_id in message
        assert "research" in message
        assert "user_approval" in message

        # The alert is keyed to the task and is open.
        alerts = msg_store.query_messages(
            type="alert", scope="architect-demo", state="open", limit=50,
        )
        senders = [a.get("sender") for a in alerts]
        assert f"state_drift:{task_id}" in senders
        alert_row = next(a for a in alerts if a.get("sender") == f"state_drift:{task_id}")
        assert (alert_row.get("payload") or {}).get("severity") == "warn"
        assert alert_row.get("state") == "open"

    def test_repeated_sweep_dedupes_alert(
        self, tmp_path: Path, monkeypatch,
    ):
        """Calling the sweep twice in a row must not raise a second
        alert — ``upsert_alert`` owns the (session, alert_type) dedupe."""
        bus.clear_listeners()
        seed = SQLiteWorkService(
            db_path=tmp_path / "work.db",
            project_path=tmp_path,
        )
        _claim_plan_task(seed, "demo")
        seed.close()

        _write_plan(tmp_path / "docs" / "plan" / "plan.md")
        store = StateStore(tmp_path / "state.db")
        from pollypm.store import SQLAlchemyStore
        msg_store = SQLAlchemyStore(f"sqlite:///{tmp_path / 'state.db'}")
        _record_plan_ready_notify(store, project="demo")

        svc = FakeSessionService(handles=[FakeHandle("architect-demo")])
        self._patch_resolver_with_factory(
            monkeypatch, tmp_path, svc, store, msg_store=msg_store,
        )

        result1 = work_progress_sweep_handler({})
        assert result1["drift_alerted"] == 1
        result2 = work_progress_sweep_handler({})
        # drift_detected can increment every sweep (the condition still
        # holds), but drift_alerted must stay at 0 on the second sweep
        # because the alert row already exists.
        assert result2["drift_alerted"] == 0

        # Only one open alert row for the task.
        # #349: alerts live in ``messages`` now.
        alerts = msg_store.query_messages(
            type="alert", scope="architect-demo", state="open", limit=50,
        )
        drift_alerts = [
            a for a in alerts
            if str(a.get("sender") or "").startswith("state_drift:")
        ]
        assert len(drift_alerts) == 1

    def test_no_drift_when_no_deliverables(
        self, tmp_path: Path, monkeypatch,
    ):
        """A plan_project task on research with nothing on disk must
        not trigger drift — the task really is upstream."""
        bus.clear_listeners()
        seed = SQLiteWorkService(
            db_path=tmp_path / "work.db",
            project_path=tmp_path,
        )
        _claim_plan_task(seed, "demo")
        seed.close()

        # No plan file, no notify.
        store = StateStore(tmp_path / "state.db")
        svc = FakeSessionService(handles=[FakeHandle("architect-demo")])
        self._patch_resolver_with_factory(monkeypatch, tmp_path, svc, store)

        result = work_progress_sweep_handler({})
        assert result["drift_detected"] == 0
        assert result["drift_alerted"] == 0

    def test_non_plan_flow_no_drift(self, tmp_path: Path, monkeypatch):
        """A standard-flow task with a random plan.md in the tree must
        not trigger drift — the heuristic scopes to plan_project."""
        bus.clear_listeners()
        seed = SQLiteWorkService(
            db_path=tmp_path / "work.db",
            project_path=tmp_path,
        )
        # Standard flow, not plan_project — test #6 in the spec.
        task = seed.create(
            title="Do the thing",
            description="desc",
            type="task",
            project="demo",
            flow_template="standard",
            roles={"worker": "agent-1", "reviewer": "agent-2"},
            priority="normal",
        )
        seed.queue(task.task_id, "pm")
        seed.claim(task.task_id, "agent-1")
        seed.close()

        # Even with an incidental plan file + notify on disk, the
        # standard flow shouldn't trigger drift.
        _write_plan(tmp_path / "docs" / "plan" / "plan.md")
        store = StateStore(tmp_path / "state.db")
        _record_plan_ready_notify(store, project="demo")

        svc = FakeSessionService(handles=[FakeHandle("worker-demo")])
        self._patch_resolver_with_factory(monkeypatch, tmp_path, svc, store)

        result = work_progress_sweep_handler({})
        assert result["drift_detected"] == 0
        assert result["drift_alerted"] == 0

    def test_active_turn_still_runs_drift_but_sweep_skips_session(
        self, tmp_path: Path, monkeypatch,
    ):
        """When the target session is actively turning, the sweep
        skips it entirely — no drift event fires for a live turn.
        This mirrors the ``stuck_on_task`` behaviour: don't touch
        sessions that are still working."""
        bus.clear_listeners()
        seed = SQLiteWorkService(
            db_path=tmp_path / "work.db",
            project_path=tmp_path,
        )
        _claim_plan_task(seed, "demo")
        seed.close()

        _write_plan(tmp_path / "docs" / "plan" / "plan.md")
        store = StateStore(tmp_path / "state.db")
        _record_plan_ready_notify(store, project="demo")

        svc = FakeSessionService(
            handles=[FakeHandle("architect-demo")],
            busy={"architect-demo"},
        )
        self._patch_resolver_with_factory(monkeypatch, tmp_path, svc, store)

        result = work_progress_sweep_handler({})
        assert result["skipped_active_turn"] == 1
        assert result["drift_detected"] == 0
        assert result["drift_alerted"] == 0
