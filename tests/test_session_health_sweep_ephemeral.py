"""Tests for the ephemeral-session pass of ``session.health_sweep`` (#252).

The planned-session sweep already iterates ``Supervisor.plan_launches()`` /
``window_map()`` and classifies / restarts as needed. This module covers the
*second* pass: ephemeral sessions (``task-*``, ``critic_*``, ``downtime_*``)
that the launch planner doesn't know about. They get classified with
``is_ephemeral=True`` semantics — alerts are raised tied to the parent task
but no auto-restart fires.

Run with::

    HOME=/tmp/pytest-agent-ephemeral-sweep uv run pytest \
        tests/test_session_health_sweep_ephemeral.py -x
"""

from __future__ import annotations

from contextlib import contextmanager

from dataclasses import dataclass, field
from typing import Any

import pytest

from pollypm.plugins_builtin.core_recurring.plugin import (
    _ephemeral_alert_type,
    is_ephemeral_session_name,
    sweep_ephemeral_sessions,
)


def _fake_load_cm(config, store):
    """Context-manager mock matching the real @contextmanager _load_config_and_store."""
    @contextmanager
    def _cm(payload):
        yield config, store
    return _cm


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class FakeHandle:
    name: str


@dataclass
class FakeHealth:
    window_present: bool = True
    pane_dead: bool = False


@dataclass
class FakeSessionService:
    handles: list[FakeHandle]
    health_by_name: dict[str, FakeHealth] = field(default_factory=dict)

    def list(self) -> list[FakeHandle]:
        return list(self.handles)

    def health(self, name: str) -> FakeHealth:
        return self.health_by_name.get(name, FakeHealth())


@dataclass
class FakeAlert:
    session_name: str
    alert_type: str
    severity: str
    message: str


@dataclass
class FakeStore:
    alerts: list[FakeAlert] = field(default_factory=list)

    def upsert_alert(
        self, session_name: str, alert_type: str, severity: str, message: str,
    ) -> None:
        self.alerts.append(FakeAlert(session_name, alert_type, severity, message))


@dataclass
class FakeLaunch:
    session_name: str

    @property
    def session(self) -> Any:  # mimic launch.session.name access
        return _NameProxy(self.session_name)


@dataclass
class _NameProxy:
    name: str


@dataclass
class FakeSupervisor:
    """Bare-minimum supervisor stand-in for the ephemeral sweep.

    Crucially: tracks every recover_session attempt so tests can assert
    that the ephemeral path NEVER triggers it.
    """

    session_service: FakeSessionService
    planned_launches: list[FakeLaunch] = field(default_factory=list)
    recover_calls: list[tuple[str, str]] = field(default_factory=list)

    def plan_launches(self) -> list[FakeLaunch]:
        return list(self.planned_launches)

    def maybe_recover_session(
        self, launch: Any, *, failure_type: str, failure_message: str,
    ) -> None:
        self.recover_calls.append((failure_type, failure_message))


# ---------------------------------------------------------------------------
# is_ephemeral predicate
# ---------------------------------------------------------------------------


class TestEphemeralPredicate:
    @pytest.mark.parametrize(
        "name",
        [
            "task-pollypm-12",
            "task-myproj-1",
            "critic_simplicity",
            "critic_security",
            "downtime_explore_a",
            "downtime_audit",
        ],
    )
    def test_ephemeral_names_are_recognized(self, name: str) -> None:
        assert is_ephemeral_session_name(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "operator",
            "architect-pollypm",
            "worker-pollypm",
            "worker_pollypm",
            "reviewer",
            "heartbeat",
            "",
        ],
    )
    def test_planned_names_are_not_ephemeral(self, name: str) -> None:
        assert is_ephemeral_session_name(name) is False


# ---------------------------------------------------------------------------
# Alert-type derivation
# ---------------------------------------------------------------------------


class TestAlertTypeDerivation:
    def test_task_prefix_extracts_parent_task_id(self) -> None:
        assert _ephemeral_alert_type("task-pollypm-12", "missing_window") == (
            "ephemeral_session_dead:pollypm/12"
        )

    def test_task_prefix_handles_hyphenated_project(self) -> None:
        # Project name with hyphens — rpartition keeps them in the project.
        assert _ephemeral_alert_type("task-my-cool-proj-7", "pane_dead") == (
            "ephemeral_session_dead:my-cool-proj/7"
        )

    def test_task_prefix_falls_back_when_unparsable(self) -> None:
        assert _ephemeral_alert_type("task-garbage", "missing_window") == (
            "ephemeral_session_dead:task-garbage"
        )

    def test_critic_prefix_uses_critic_failed(self) -> None:
        assert _ephemeral_alert_type("critic_simplicity", "missing_window") == (
            "critic_failed:critic_simplicity"
        )

    def test_downtime_prefix_uses_downtime_failed(self) -> None:
        assert _ephemeral_alert_type("downtime_explore_a", "pane_dead") == (
            "downtime_failed:downtime_explore_a"
        )


# ---------------------------------------------------------------------------
# sweep_ephemeral_sessions
# ---------------------------------------------------------------------------


class TestSweepEphemeralSessions:
    def test_picks_up_ephemeral_sessions_only(self) -> None:
        """Planned + non-ephemeral handles are skipped; ephemeral counted."""
        svc = FakeSessionService(
            handles=[
                FakeHandle("operator"),               # planned, skip
                FakeHandle("worker-pollypm"),         # planned, skip
                FakeHandle("task-pollypm-12"),        # ephemeral, healthy
                FakeHandle("critic_simplicity"),      # ephemeral, healthy
                FakeHandle("downtime_explore_a"),     # ephemeral, healthy
            ],
        )
        supervisor = FakeSupervisor(
            session_service=svc,
            planned_launches=[
                FakeLaunch("operator"),
                FakeLaunch("worker-pollypm"),
            ],
        )
        store = FakeStore()

        summary = sweep_ephemeral_sessions(supervisor, store)

        assert summary["considered"] == 3
        # Healthy → no alerts.
        assert summary["alerts_raised"] == 0
        assert store.alerts == []

    def test_dead_pane_raises_alert_with_parent_task_key(self) -> None:
        """A task-* session with a dead pane fires ephemeral_session_dead."""
        svc = FakeSessionService(
            handles=[FakeHandle("task-pollypm-12")],
            health_by_name={
                "task-pollypm-12": FakeHealth(window_present=True, pane_dead=True),
            },
        )
        supervisor = FakeSupervisor(session_service=svc)
        store = FakeStore()

        summary = sweep_ephemeral_sessions(supervisor, store)

        assert summary["considered"] == 1
        assert summary["alerts_raised"] == 1
        assert len(store.alerts) == 1
        alert = store.alerts[0]
        assert alert.session_name == "task-pollypm-12"
        assert alert.alert_type == "ephemeral_session_dead:pollypm/12"
        assert alert.severity == "warn"
        # Three-question rule (#240): message answers what / why / how to fix.
        assert "pane has exited" in alert.message
        assert "parent task" in alert.message.lower()
        assert "fix" in alert.message.lower()

    def test_missing_window_fires_critic_failed_for_critic_session(self) -> None:
        svc = FakeSessionService(
            handles=[FakeHandle("critic_simplicity")],
            health_by_name={
                "critic_simplicity": FakeHealth(window_present=False),
            },
        )
        supervisor = FakeSupervisor(session_service=svc)
        store = FakeStore()

        summary = sweep_ephemeral_sessions(supervisor, store)

        assert summary["alerts_raised"] == 1
        assert store.alerts[0].alert_type == "critic_failed:critic_simplicity"

    def test_missing_window_fires_downtime_failed_for_downtime_session(self) -> None:
        svc = FakeSessionService(
            handles=[FakeHandle("downtime_explore_a")],
            health_by_name={
                "downtime_explore_a": FakeHealth(window_present=False),
            },
        )
        supervisor = FakeSupervisor(session_service=svc)
        store = FakeStore()

        summary = sweep_ephemeral_sessions(supervisor, store)

        assert summary["alerts_raised"] == 1
        assert store.alerts[0].alert_type == "downtime_failed:downtime_explore_a"

    def test_restart_is_suppressed_for_ephemeral_failure(self) -> None:
        """The ephemeral path must NEVER call recover_session.

        Planned sessions are auto-restarted on missing_window / pane_dead,
        but ephemeral sessions are owned by their spawning subsystem
        (work service / planner / downtime). The sweep raises an alert
        and stops there.
        """
        svc = FakeSessionService(
            handles=[
                FakeHandle("task-pollypm-12"),
                FakeHandle("critic_security"),
            ],
            health_by_name={
                "task-pollypm-12": FakeHealth(window_present=False),
                "critic_security": FakeHealth(window_present=True, pane_dead=True),
            },
        )
        supervisor = FakeSupervisor(session_service=svc)
        store = FakeStore()

        summary = sweep_ephemeral_sessions(supervisor, store)

        # Alerts fired — but no restart was attempted for any session.
        assert summary["alerts_raised"] == 2
        assert supervisor.recover_calls == []

    def test_double_count_guard_when_ephemeral_name_appears_in_plan(self) -> None:
        """If an ephemeral name happens to be in the launch plan it's skipped."""
        svc = FakeSessionService(
            handles=[FakeHandle("task-pollypm-12")],
            health_by_name={
                "task-pollypm-12": FakeHealth(window_present=False),
            },
        )
        supervisor = FakeSupervisor(
            session_service=svc,
            planned_launches=[FakeLaunch("task-pollypm-12")],
        )
        store = FakeStore()

        summary = sweep_ephemeral_sessions(supervisor, store)

        # The ephemeral pass refused to act because the planned sweep
        # already covered this name.
        assert summary["considered"] == 0
        assert summary["alerts_raised"] == 0
        assert summary["skipped_planned"] == 1
        assert store.alerts == []

    def test_session_service_failure_is_swallowed(self) -> None:
        """A misbehaving SessionService must not crash the sweep."""

        class BoomService:
            def list(self) -> list[FakeHandle]:
                raise RuntimeError("boom")

            def health(self, name: str) -> FakeHealth:
                return FakeHealth()

        supervisor = FakeSupervisor(session_service=BoomService())
        store = FakeStore()

        summary = sweep_ephemeral_sessions(supervisor, store)

        assert summary == {
            "considered": 0, "alerts_raised": 0, "skipped_planned": 0,
        }

    def test_per_session_health_failure_does_not_abort_sweep(self) -> None:
        """A health() exception on session N must not skip session N+1."""

        class FlakyService:
            def __init__(self) -> None:
                self.handles_list = [
                    FakeHandle("task-bad-1"),
                    FakeHandle("task-good-2"),
                ]

            def list(self) -> list[FakeHandle]:
                return list(self.handles_list)

            def health(self, name: str) -> FakeHealth:
                if name == "task-bad-1":
                    raise RuntimeError("flaky")
                return FakeHealth(window_present=False)

        supervisor = FakeSupervisor(session_service=FlakyService())
        store = FakeStore()

        summary = sweep_ephemeral_sessions(supervisor, store)

        # Both considered; only the second produced an alert.
        assert summary["considered"] == 2
        assert summary["alerts_raised"] == 1
        assert store.alerts[0].session_name == "task-good-2"


# ---------------------------------------------------------------------------
# Handler integration — ephemeral counts surface in the summary
# ---------------------------------------------------------------------------


class TestHandlerSurfacesEphemeralCounts:
    def test_summary_includes_ephemeral_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """End-to-end: handler attaches ephemeral_* counters to its result."""
        from pollypm.plugins_builtin.core_recurring import plugin as mod

        # Stub the supervisor sweep so we don't need a real config / tmux.
        class StubSupervisor:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                self.session_service = FakeSessionService(
                    handles=[
                        FakeHandle("task-pollypm-12"),
                        FakeHandle("critic_simplicity"),
                    ],
                    health_by_name={
                        "task-pollypm-12": FakeHealth(window_present=False),
                    },
                )

            def run_heartbeat(self, *, snapshot_lines: int = 200) -> list[Any]:
                return []  # no planned alerts

            def plan_launches(self) -> list[FakeLaunch]:
                return []

            def maybe_recover_session(self, *_args: Any, **_kwargs: Any) -> None:
                raise AssertionError(
                    "ephemeral sweep must not auto-recover sessions",
                )

        store = FakeStore()

        # The handler loads config via _load_config_and_store. Stub that
        # so the test stays hermetic.
        monkeypatch.setattr(
            mod, "_load_config_and_store",
            _fake_load_cm(object(), store),
        )
        monkeypatch.setattr(mod, "Supervisor", StubSupervisor, raising=False)
        # The handler imports Supervisor inside the function body. Patch
        # the module's import site by injecting a fake module attribute
        # on pollypm.supervisor.
        import pollypm.supervisor as supervisor_module
        monkeypatch.setattr(
            supervisor_module, "Supervisor", StubSupervisor,
        )

        result = mod.session_health_sweep_handler({})

        assert result["alerts_raised"] == 0     # planned sweep: zero alerts
        assert result["ephemeral_considered"] == 2
        assert result["ephemeral_alerts_raised"] == 1
        assert result["ephemeral_skipped_planned"] == 0
        # Confirm the alert was the missing-window one for the task.
        assert any(
            a.alert_type == "ephemeral_session_dead:pollypm/12"
            for a in store.alerts
        )
