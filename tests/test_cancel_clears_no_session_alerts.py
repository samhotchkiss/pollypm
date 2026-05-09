"""Regression tests for issue #927 — cancelled tasks keep raising alerts.

Background:
- ``pm task cancel <project>/<n> --reason "..."`` flips the task status
  to ``cancelled`` but did NOT clear the
  ``task_assignment/no_session_for_assignment:<project>/<n>`` and
  ``worker-<project>/no_session`` alerts that the heartbeat sweep raised
  while the task was active.
- The supervisor's ``_sweep_stale_alerts`` was tightened in #919 to NOT
  clear ``no_session`` types prematurely (the alert clearer was masking
  #919's real bug). The other side of that coin is that cancelled-task
  alerts now stick around because no-one prunes them on cancel.

Two-part fix exercised here:
  Part A — cancel clears its own alerts (per-task always; project-level
  only when no other active task on the project still routes to that
  role).
  Part B — heartbeat sweep refuses to emit ``no_session_for_assignment``
  for terminal / parked status tasks (cancelled / done / on_hold /
  blocked / draft). Active-status tasks still ping normally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pollypm.plugins_builtin.task_assignment_notify.handlers.sweep import (
    task_assignment_sweep_handler,
)
from pollypm.plugins_builtin.task_assignment_notify.resolver import (
    _RuntimeServices,
    clear_alerts_for_cancelled_task,
    clear_no_session_alert_for_task,
)
from pollypm.work.models import (
    Artifact,
    ArtifactKind,
    OutputType,
    WorkOutput,
)
from pollypm.storage.state import StateStore
from pollypm.work import task_assignment as bus
from pollypm.work import task_assignment_alerts as alert_bus
from pollypm.work.sqlite_service import SQLiteWorkService


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _FakeHandle:
    name: str


@dataclass
class _FakeSessionService:
    handles: list[_FakeHandle]
    sent: list[tuple[str, str]] = field(default_factory=list)
    busy: set[str] = field(default_factory=set)

    def list(self) -> list[_FakeHandle]:
        return list(self.handles)

    def send(self, name: str, text: str, *, press_enter: bool = True) -> None:
        self.sent.append((name, text))

    def is_turn_active(self, name: str) -> bool:
        return name in self.busy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_environment(tmp_path: Path) -> tuple[SQLiteWorkService, StateStore]:
    bus.clear_listeners()
    alert_bus.clear_listeners()
    work = SQLiteWorkService(db_path=tmp_path / "work.db")
    store = StateStore(tmp_path / "state.db")
    return work, store


def _create_worker_task(
    work: SQLiteWorkService, *, project: str, title: str,
):
    return work.create(
        title=title,
        description="Implement the thing",
        type="task",
        project=project,
        flow_template="standard",
        roles={"worker": "worker", "reviewer": "reviewer"},
        priority="normal",
    )


def _claim_worker_task(work: SQLiteWorkService, *, project: str, title: str):
    task = _create_worker_task(work, project=project, title=title)
    work.queue(task.task_id, "pm")
    work.claim(task.task_id, "worker")
    return work.get(task.task_id)


def _drive_task_to_review(
    work: SQLiteWorkService, *, project: str, title: str,
):
    """Create, queue, claim, and ``node_done`` a worker task so it lands
    in ``review`` ready for ``approve``. Used by the #953 regression
    tests that need an actually-in-review task — the cancel tests above
    happily work on plain in_progress tasks, but the approve transition
    only fires on the review state.
    """
    task = _claim_worker_task(work, project=project, title=title)
    work.node_done(
        task.task_id,
        "worker",
        WorkOutput(
            type=OutputType.CODE_CHANGE,
            summary="implemented the thing",
            artifacts=[
                Artifact(
                    kind=ArtifactKind.COMMIT,
                    description="feat: did the work",
                    ref="abc123",
                ),
            ],
        ),
    )
    return work.get(task.task_id)


def _open_alerts(store: StateStore) -> list[tuple[str, str]]:
    """Return ``(session_name, alert_type)`` for every open alert."""
    return [
        (alert.session_name, alert.alert_type) for alert in store.open_alerts()
    ]


def _install_resolver_loader(monkeypatch, services: _RuntimeServices) -> None:
    """Make ``clear_alerts_for_cancelled_task`` use the supplied services.

    Also short-circuits ``WorkTransitionManager._resolve_alert_store`` so
    the cancel-time hook bypasses the production config-file path and
    routes the test's StateStore directly into the resolver. Returning
    ``None`` here forces the helper to call back through
    ``load_runtime_services`` (which we've monkeypatched), keeping the
    test self-contained.
    """
    monkeypatch.setattr(
        "pollypm.plugins_builtin.task_assignment_notify.resolver.load_runtime_services",
        lambda *, config_path=None: services,
    )
    monkeypatch.setattr(
        "pollypm.work.service_transition_manager.WorkTransitionManager._resolve_alert_store",
        staticmethod(lambda: None),
    )
    from pollypm.plugins_builtin.task_assignment_notify.plugin import (
        _assignment_alert_cleanup_listener,
    )

    alert_bus.register_listener(_assignment_alert_cleanup_listener)


def _install_sweep_loader(monkeypatch, services: _RuntimeServices) -> None:
    monkeypatch.setattr(
        "pollypm.plugins_builtin.task_assignment_notify.handlers.sweep.load_runtime_services",
        lambda *, config_path=None: services,
    )


# ---------------------------------------------------------------------------
# Part A — cancel clears the alerts it should clear
# ---------------------------------------------------------------------------


class TestCancelClearsNoSessionAlerts:
    def test_cancel_only_active_task_clears_per_task_and_project_alerts(
        self, tmp_path, monkeypatch,
    ):
        """Cancelling the only in_progress task on a project clears both
        the per-task ``no_session_for_assignment:<id>`` alert and the
        project-level ``(worker-<project>, no_session)`` alert.
        """
        work, store = _make_environment(tmp_path)
        task = _claim_worker_task(
            work, project="blackjack-trainer", title="Add charts",
        )

        # Pre-seed the alerts the sweep would have raised while the task
        # was active. Both alert families live in the unified messages
        # table via ``upsert_alert``.
        store.upsert_alert(
            "task_assignment",
            f"no_session_for_assignment:{task.task_id}",
            "warning",
            "Task blackjack-trainer/1 was routed to the worker role but no "
            "matching session is running.",
        )
        store.upsert_alert(
            "worker-blackjack-trainer",
            "no_session",
            "warn",
            "No worker is running for the worker role on 'blackjack-trainer'.",
        )

        # The resolver helper only needs the alert store — pass
        # work_service=None so its incidental-close logic doesn't shut
        # down the cancel call's own connection.
        services = _RuntimeServices(
            session_service=None, state_store=store,
            work_service=None, project_root=tmp_path,
            msg_store=store,
        )
        _install_resolver_loader(monkeypatch, services)

        # Cancel the task — the cancel hook should invoke
        # ``clear_alerts_for_cancelled_task`` and walk both alert keys.
        work.cancel(task.task_id, "tester", reason="no longer needed")

        open_keys = _open_alerts(store)
        assert (
            "task_assignment", f"no_session_for_assignment:{task.task_id}",
        ) not in open_keys, (
            f"per-task alert should be cleared; saw {open_keys!r}"
        )
        assert (
            "worker-blackjack-trainer", "no_session",
        ) not in open_keys, (
            "project-level worker alert should clear when the cancelled "
            "task was the only active task on the project; "
            f"saw {open_keys!r}"
        )

    def test_cancel_one_of_many_keeps_project_alert_clears_per_task_alert(
        self, tmp_path, monkeypatch,
    ):
        """Cancelling one task on a project that still has another active
        task: the per-task alert clears for the cancelled one, but the
        project-level worker alert stays open because another in_progress
        task on the same project still needs that role.
        """
        work, store = _make_environment(tmp_path)
        task_a = _claim_worker_task(
            work, project="blackjack-trainer", title="Implement A",
        )
        task_b = _claim_worker_task(
            work, project="blackjack-trainer", title="Implement B",
        )

        # Per-task alerts for each + a single shared project-level alert.
        store.upsert_alert(
            "task_assignment",
            f"no_session_for_assignment:{task_a.task_id}",
            "warning", "Task A no session.",
        )
        store.upsert_alert(
            "task_assignment",
            f"no_session_for_assignment:{task_b.task_id}",
            "warning", "Task B no session.",
        )
        store.upsert_alert(
            "worker-blackjack-trainer", "no_session",
            "warn", "No worker running.",
        )

        # The resolver helper only needs the alert store — pass
        # work_service=None so its incidental-close logic doesn't shut
        # down the cancel call's own connection.
        services = _RuntimeServices(
            session_service=None, state_store=store,
            work_service=None, project_root=tmp_path,
            msg_store=store,
        )
        _install_resolver_loader(monkeypatch, services)

        work.cancel(task_a.task_id, "tester", reason="dropped")

        open_keys = _open_alerts(store)
        # Per-task alert for the cancelled task is gone.
        assert (
            "task_assignment", f"no_session_for_assignment:{task_a.task_id}",
        ) not in open_keys
        # Per-task alert for the still-active task survives.
        assert (
            "task_assignment", f"no_session_for_assignment:{task_b.task_id}",
        ) in open_keys
        # Project-level alert MUST remain — task_b is still in_progress.
        assert (
            "worker-blackjack-trainer", "no_session",
        ) in open_keys, (
            "project-level worker alert must remain open while another "
            f"active task on the project still routes to worker; got {open_keys!r}"
        )

    def test_cancel_only_active_with_blocked_sibling_clears_project_alert(
        self, tmp_path, monkeypatch,
    ):
        """#941 — cancelling the only active worker task on a project
        whose only remaining worker-role sibling is ``blocked`` must
        clear BOTH the per-task alert and the project-level
        ``worker-<project>/no_session`` alert.

        Rationale: the heartbeat sweep refuses to raise
        ``no_session_for_assignment`` for blocked tasks (sweep contract
        in ``_NON_ACTIVE_SWEEP_STATUSES`` — blocked tasks have their own
        gate-blocked alert family). So a blocked sibling cannot keep the
        project-level alert refreshed; leaving it open after cancel is
        pure noise.
        """
        work, store = _make_environment(tmp_path)
        to_cancel = _claim_worker_task(
            work, project="demo", title="active to cancel",
        )
        # Set up a sibling that is blocked on another task. ``block``
        # requires a blocker task id, so build a throwaway blocker that
        # is itself unrelated to the worker role decision.
        sibling = _claim_worker_task(
            work, project="demo", title="blocked sibling",
        )
        blocker = _create_worker_task(
            work, project="demo", title="the blocker",
        )
        work.block(sibling.task_id, "tester", blocker.task_id)

        store.upsert_alert(
            "task_assignment",
            f"no_session_for_assignment:{to_cancel.task_id}",
            "warning", "Task no session.",
        )
        store.upsert_alert(
            "worker-demo", "no_session",
            "warn", "No worker running.",
        )

        services = _RuntimeServices(
            session_service=None, state_store=store,
            work_service=None, project_root=tmp_path,
            msg_store=store,
        )
        _install_resolver_loader(monkeypatch, services)

        work.cancel(to_cancel.task_id, "tester", reason="dropped")

        open_keys = _open_alerts(store)
        # Per-task alert for the cancelled task is gone.
        assert (
            "task_assignment", f"no_session_for_assignment:{to_cancel.task_id}",
        ) not in open_keys, (
            f"per-task alert should be cleared; saw {open_keys!r}"
        )
        # Project-level alert MUST clear — the only remaining sibling is
        # blocked, which the sweep won't ping for, so keeping the
        # ``worker-demo/no_session`` alert open is stale noise.
        assert (
            "worker-demo", "no_session",
        ) not in open_keys, (
            "#941: project-level worker alert must clear when the only "
            "remaining worker-role sibling is blocked (sweep won't "
            f"re-emit for blocked tasks); saw {open_keys!r}"
        )


class TestClearHelperRespectsHasOtherActive:
    """Direct unit tests on ``clear_alerts_for_cancelled_task``."""

    def test_clears_project_alert_when_no_other_active(
        self, tmp_path, monkeypatch,
    ):
        bus.clear_listeners()
        alert_bus.clear_listeners()
        store = StateStore(tmp_path / "state.db")
        store.upsert_alert(
            "task_assignment",
            "no_session_for_assignment:demo/1",
            "warning", "msg",
        )
        store.upsert_alert(
            "worker-demo", "no_session", "warn", "msg",
        )
        services = _RuntimeServices(
            session_service=None, state_store=store,
            work_service=None, project_root=tmp_path, msg_store=store,
        )
        _install_resolver_loader(monkeypatch, services)

        result = clear_alerts_for_cancelled_task(
            task_id="demo/1", project="demo",
            role_names=("worker",),
            has_other_active_for_role={"worker": False},
        )

        assert result["cleared_per_task"] is True
        assert "worker-demo" in result["cleared_project"]
        assert _open_alerts(store) == []

    def test_skips_project_alert_when_other_active(
        self, tmp_path, monkeypatch,
    ):
        bus.clear_listeners()
        alert_bus.clear_listeners()
        store = StateStore(tmp_path / "state.db")
        store.upsert_alert(
            "task_assignment",
            "no_session_for_assignment:demo/1",
            "warning", "msg",
        )
        store.upsert_alert(
            "worker-demo", "no_session", "warn", "msg",
        )
        services = _RuntimeServices(
            session_service=None, state_store=store,
            work_service=None, project_root=tmp_path, msg_store=store,
        )
        _install_resolver_loader(monkeypatch, services)

        result = clear_alerts_for_cancelled_task(
            task_id="demo/1", project="demo",
            role_names=("worker",),
            has_other_active_for_role={"worker": True},
        )

        assert result["cleared_per_task"] is True
        # Project-level alert is left alone — another active task still
        # needs the worker role on this project.
        assert result["cleared_project"] == []
        open_keys = _open_alerts(store)
        assert ("worker-demo", "no_session") in open_keys


# ---------------------------------------------------------------------------
# Part B — sweep skips terminal-status tasks
# ---------------------------------------------------------------------------


class TestSweepSkipsTerminalStatusTasks:
    def test_sweep_emits_for_active_tasks(self, tmp_path, monkeypatch):
        """Sanity check — an in_progress task with no live session
        produces the expected ``no_session`` outcome and project-level
        alert. This anchors the contract before the negative test below.
        """
        work, store = _make_environment(tmp_path)
        task = _claim_worker_task(
            work, project="demo", title="Active work",
        )
        # No live session whatsoever — every notify will escalate.
        svc = _FakeSessionService(handles=[])
        services = _RuntimeServices(
            session_service=svc, state_store=store,
            work_service=work, project_root=tmp_path, msg_store=store,
        )
        _install_sweep_loader(monkeypatch, services)

        result = task_assignment_sweep_handler({})

        assert result["by_outcome"].get("no_session", 0) >= 1
        # Sweep raised a project-level alert for the missing role.
        open_keys = _open_alerts(store)
        per_task_alerts = [
            key for key in open_keys
            if key[1] == f"no_session_for_assignment:{task.task_id}"
        ]
        assert per_task_alerts, (
            "expected sweep to raise a per-task no_session alert for the "
            f"active task; got {open_keys!r}"
        )

    def test_sweep_silent_for_cancelled_done_on_hold_only_project(
        self, tmp_path, monkeypatch,
    ):
        """A project whose only tasks are non-active (cancelled / done /
        on_hold) raises ZERO ``no_session_for_assignment`` alerts.
        """
        work, store = _make_environment(tmp_path)
        # Cancelled task.
        cancelled = _claim_worker_task(
            work, project="demo", title="Was active, now cancelled",
        )
        work.cancel(cancelled.task_id, "tester", reason="abandoned")
        # On-hold task.
        on_hold = _claim_worker_task(
            work, project="demo", title="Was active, now parked",
        )
        work.hold(on_hold.task_id, "tester", reason="parked")

        # Pre-clear any alerts the cancel side-effect may have left so we
        # measure only the sweep's behaviour from this point on.
        store.clear_alert(
            "task_assignment",
            f"no_session_for_assignment:{cancelled.task_id}",
        )
        store.clear_alert(
            "task_assignment",
            f"no_session_for_assignment:{on_hold.task_id}",
        )
        store.clear_alert("worker-demo", "no_session")

        svc = _FakeSessionService(handles=[])
        services = _RuntimeServices(
            session_service=svc, state_store=store,
            work_service=work, project_root=tmp_path, msg_store=store,
        )
        _install_sweep_loader(monkeypatch, services)

        result = task_assignment_sweep_handler({})

        # Nothing in this project is active — sweep must consider zero
        # tasks and raise zero alerts.
        assert result["considered"] == 0, (
            f"expected sweep to consider zero tasks; got {result!r}"
        )
        open_keys = _open_alerts(store)
        for session_name, alert_type in open_keys:
            assert not alert_type.startswith("no_session_for_assignment:"), (
                f"sweep emitted a no_session_for_assignment alert for a "
                f"non-active task: {session_name}/{alert_type}"
            )
            assert alert_type != "no_session", (
                f"sweep emitted a project-level no_session alert when no "
                f"active tasks exist: {session_name}/{alert_type}"
            )

    def test_sweep_emits_for_queued_review_inprogress(self, tmp_path, monkeypatch):
        """Active statuses (queued / in_progress / review) keep firing.

        Guards against an over-broad terminal-status filter that would
        suppress the legitimate ping path. Each of the three states
        below is ``_SWEEPABLE_STATUSES`` and must produce a
        ``no_session_for_assignment`` alert when no live session exists.
        """
        work, store = _make_environment(tmp_path)
        # Queued task.
        queued = _create_worker_task(
            work, project="demo", title="Queued work",
        )
        work.queue(queued.task_id, "pm")
        # in_progress task (claim moves to in_progress).
        in_progress = _claim_worker_task(
            work, project="demo", title="In-flight work",
        )
        # review task — claim then submit_for_review (if available).
        # Easier: build a separate task and walk it via complete_node.
        # The kickoff sweep tests don't exercise ``review`` directly,
        # so we settle for queued + in_progress here as a stand-in for
        # the active-status contract; a dedicated review state test
        # would require flow-engine setup beyond this regression's scope.
        svc = _FakeSessionService(handles=[])
        services = _RuntimeServices(
            session_service=svc, state_store=store,
            work_service=work, project_root=tmp_path, msg_store=store,
        )
        _install_sweep_loader(monkeypatch, services)

        result = task_assignment_sweep_handler({})

        assert result["considered"] >= 2, (
            f"expected sweep to consider at least the queued + in_progress "
            f"tasks; got {result!r}"
        )
        open_keys = _open_alerts(store)
        per_task_alerts = {
            key[1] for key in open_keys
            if key[1].startswith("no_session_for_assignment:")
        }
        assert (
            f"no_session_for_assignment:{queued.task_id}" in per_task_alerts
        )
        assert (
            f"no_session_for_assignment:{in_progress.task_id}" in per_task_alerts
        )


# ---------------------------------------------------------------------------
# #919 stale-alert clearer guard intact
# ---------------------------------------------------------------------------


class TestStaleAlertGuardIntact:
    def test_supervisor_sweep_stale_alerts_skips_no_session_types(self):
        """``Supervisor._sweep_stale_alerts`` (#919) must continue to
        skip both ``no_session`` and ``no_session_for_assignment:*``
        alert types — the owning task_assignment sweep is the sole
        clearer of those. Regressing this guard would re-introduce
        the false-clean #919 was written to fix.

        We assert against the source code rather than a runtime fixture
        so future refactors that move the guard still trip this test
        if the guard is removed.
        """
        from pollypm import supervisor as supervisor_mod
        import inspect

        source = inspect.getsource(supervisor_mod.Supervisor._sweep_stale_alerts)

        # Both predicate halves must be present and short-circuit before
        # ``clear_alert`` is called.
        assert 'alert_type == "no_session"' in source, (
            "#919 guard for no_session alert_type missing from "
            "_sweep_stale_alerts — the task_assignment sweep is the sole "
            "owner of these alerts and must keep clearing them itself."
        )
        assert 'alert_type.startswith("no_session_for_assignment:")' in source, (
            "#919 guard for no_session_for_assignment:* alert_type "
            "missing from _sweep_stale_alerts."
        )

    def test_supervisor_sweep_stale_alerts_skips_plan_missing(self):
        """``Supervisor._sweep_stale_alerts`` (#1526) must skip
        ``plan_missing`` alerts. They are emitted by the task_assignment
        sweep on the synthetic ``plan_gate-<project>`` session name,
        which is intentionally never in the launch plan. Regressing
        this guard re-introduces the #1524 banner flash: the heartbeat
        closes the alert every tick and the task_assignment sweep
        re-emits it ~30s later.
        """
        from pollypm import supervisor as supervisor_mod
        import inspect

        source = inspect.getsource(supervisor_mod.Supervisor._sweep_stale_alerts)

        assert 'alert_type == "plan_missing"' in source, (
            "#1526 guard for plan_missing alert_type missing from "
            "_sweep_stale_alerts — the task_assignment sweep is the sole "
            "owner of these alerts and must keep clearing them itself. "
            "Without this guard the #1524 banner flash returns."
        )


# ---------------------------------------------------------------------------
# #953 — approve clears the per-task no_session alert on review-exit
# ---------------------------------------------------------------------------


class TestApproveClearsNoSessionAlert:
    """The CLI approve path is the canonical reviewer action. The
    heartbeat sweep raises ``no_session_for_assignment:<task_id>`` while
    the task sits in ``review`` waiting for a reviewer session. The
    moment ``approve`` succeeds, that alert is stale — clear it
    synchronously instead of waiting for a later sweep tick to notice
    (#953 reopen).
    """

    def test_approve_clears_no_session_alert_for_reviewer(
        self, tmp_path, monkeypatch,
    ):
        """Set up: task sitting in ``review`` with a per-task
        ``no_session_for_assignment:proj/1`` alert open. Run approve via
        the work service. Assert: the per-task alert is no longer in the
        open-alerts list.
        """
        work, store = _make_environment(tmp_path)
        task = _drive_task_to_review(
            work, project="demo", title="Reviewable task",
        )

        # Pre-seed the per-task alert the heartbeat sweep would have
        # raised while the task was waiting for a reviewer session.
        store.upsert_alert(
            "task_assignment",
            f"no_session_for_assignment:{task.task_id}",
            "warning",
            "Task demo/1 was routed to the reviewer role but no "
            "matching session is running.",
        )

        services = _RuntimeServices(
            session_service=None, state_store=store,
            work_service=None, project_root=tmp_path,
            msg_store=store,
        )
        _install_resolver_loader(monkeypatch, services)

        # Approve — the post-transition hook should walk the same
        # plugin public API surface the cancel path uses (#939) and
        # close out the per-task alert.
        work.approve(task.task_id, "reviewer", reason="LGTM")

        open_keys = _open_alerts(store)
        assert (
            "task_assignment", f"no_session_for_assignment:{task.task_id}",
        ) not in open_keys, (
            "approve should clear the per-task no_session_for_assignment "
            f"alert as part of the review-exit transition; saw {open_keys!r}"
        )

    def test_approve_leaves_unrelated_per_task_alert_open(
        self, tmp_path, monkeypatch,
    ):
        """Approving task A must not touch task B's per-task alert. The
        helper keys on the just-approved task id, so a sibling task
        sitting in review with its own no-session alert should still
        have that alert open after we approve A.
        """
        work, store = _make_environment(tmp_path)
        task_a = _drive_task_to_review(
            work, project="demo", title="A",
        )
        task_b = _drive_task_to_review(
            work, project="demo", title="B",
        )
        store.upsert_alert(
            "task_assignment",
            f"no_session_for_assignment:{task_a.task_id}",
            "warning", "A no session.",
        )
        store.upsert_alert(
            "task_assignment",
            f"no_session_for_assignment:{task_b.task_id}",
            "warning", "B no session.",
        )

        services = _RuntimeServices(
            session_service=None, state_store=store,
            work_service=None, project_root=tmp_path, msg_store=store,
        )
        _install_resolver_loader(monkeypatch, services)

        work.approve(task_a.task_id, "reviewer", reason="LGTM")

        open_keys = _open_alerts(store)
        assert (
            "task_assignment", f"no_session_for_assignment:{task_a.task_id}",
        ) not in open_keys
        assert (
            "task_assignment", f"no_session_for_assignment:{task_b.task_id}",
        ) in open_keys, (
            "approving task A must NOT clear task B's per-task alert; "
            f"saw {open_keys!r}"
        )


class TestClearNoSessionAlertHelper:
    """Direct unit tests on ``clear_no_session_alert_for_task``."""

    def test_clears_per_task_alert_only(self, tmp_path, monkeypatch):
        bus.clear_listeners()
        alert_bus.clear_listeners()
        store = StateStore(tmp_path / "state.db")
        store.upsert_alert(
            "task_assignment",
            "no_session_for_assignment:demo/1",
            "warning", "msg",
        )
        # Project-level alert MUST survive — approve doesn't necessarily
        # mean the role is no longer needed on the project.
        store.upsert_alert(
            "worker-demo", "no_session", "warn", "msg",
        )
        services = _RuntimeServices(
            session_service=None, state_store=store,
            work_service=None, project_root=tmp_path, msg_store=store,
        )
        _install_resolver_loader(monkeypatch, services)

        result = clear_no_session_alert_for_task(task_id="demo/1")

        assert result["cleared_per_task"] is True
        open_keys = _open_alerts(store)
        assert (
            "task_assignment", "no_session_for_assignment:demo/1",
        ) not in open_keys
        assert ("worker-demo", "no_session") in open_keys, (
            "project-level alert must NOT be cleared by the approve-side "
            "helper; only the per-task alert is unambiguously stale"
        )


class TestPluginAlertCleanupSubscription:
    def test_initialize_subscribes_to_alert_cleanup_bus(
        self, tmp_path, monkeypatch,
    ):
        bus.clear_listeners()
        alert_bus.clear_listeners()
        from pollypm.session_services import base as session_bus

        session_bus.clear_session_listeners()
        store = StateStore(tmp_path / "state.db")
        store.upsert_alert(
            "task_assignment",
            "no_session_for_assignment:demo/1",
            "warning",
            "msg",
        )
        services = _RuntimeServices(
            session_service=None,
            state_store=store,
            work_service=None,
            project_root=tmp_path,
            msg_store=store,
        )
        monkeypatch.setattr(
            "pollypm.plugins_builtin.task_assignment_notify.resolver.load_runtime_services",
            lambda *, config_path=None: services,
        )

        from pollypm.plugin_api.v1 import PluginAPI
        from pollypm.plugins_builtin.task_assignment_notify.plugin import _initialize

        _initialize(
            PluginAPI(
                plugin_name="task_assignment_notify",
                roster_api=None,
                jobs_api=None,
            )
        )
        alert_bus.dispatch(
            alert_bus.ClearNoSessionAlertForTaskEvent(task_id="demo/1")
        )

        assert (
            "task_assignment",
            "no_session_for_assignment:demo/1",
        ) not in _open_alerts(store)
        bus.clear_listeners()
        alert_bus.clear_listeners()
        session_bus.clear_session_listeners()
