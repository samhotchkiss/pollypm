"""Issue #2068 — sessions-admin pause marker wired into recovery loops.

Covers the **partial** wiring in PR ``feat/sessions-pause-marker-wire-loops-1-3``:

1. ``pollypm.session_paused`` reader contract (``is_paused`` /
   ``load_paused_names`` / ``skip_if_paused``).
2. ``pollypm.recovery.no_session_spawn.auto_recover_no_session_alerts``
   honours the marker — no spawn fires for a paused expected-session.
3. ``pollypm.supervisor.Supervisor.maybe_recover_session`` honours the
   marker — no policy-recommendation / restart side effects when the
   session is paused.

Remaining loops (core_recurring / cockpit_rail / heartbeats) are
tracked under #2068 as follow-ups and are NOT exercised here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Fixture: a config-shaped object with a writable base_dir
# ---------------------------------------------------------------------------


@dataclass
class _FakeProject:
    base_dir: Path
    key: str = "demo"


@dataclass
class _FakeConfig:
    project: _FakeProject


@pytest.fixture
def config_with_base_dir(tmp_path: Path) -> _FakeConfig:
    base = tmp_path / "base"
    base.mkdir()
    return _FakeConfig(project=_FakeProject(base_dir=base))


def _write_marker(config: _FakeConfig, names: list[str]) -> Path:
    """Write the pause marker the way ``sessions_admin._write_paused_names``
    would — a JSON list of names sitting at ``<base_dir>/paused-sessions.json``."""
    path = config.project.base_dir / "paused-sessions.json"
    path.write_text(json.dumps(sorted(names), indent=2) + "\n")
    return path


# ---------------------------------------------------------------------------
# Unit — session_paused reader
# ---------------------------------------------------------------------------


def test_load_paused_names_returns_empty_when_marker_missing(
    config_with_base_dir: _FakeConfig,
) -> None:
    from pollypm.session_paused import load_paused_names

    # No file written yet → empty set, not an exception.
    assert load_paused_names(config_with_base_dir) == set()


def test_load_paused_names_reads_marker(config_with_base_dir: _FakeConfig) -> None:
    from pollypm.session_paused import load_paused_names

    _write_marker(config_with_base_dir, ["operator", "reviewer-demo"])
    assert load_paused_names(config_with_base_dir) == {
        "operator", "reviewer-demo",
    }


def test_load_paused_names_handles_malformed_json(
    config_with_base_dir: _FakeConfig,
) -> None:
    from pollypm.session_paused import load_paused_names

    (config_with_base_dir.project.base_dir / "paused-sessions.json").write_text(
        "{not json}"
    )
    # Best-effort: malformed file collapses to empty so the loops gate
    # on it can't crash the daemon.
    assert load_paused_names(config_with_base_dir) == set()


def test_load_paused_names_handles_no_base_dir() -> None:
    from pollypm.session_paused import load_paused_names

    class _NullProject:
        base_dir = None

    class _NullConfig:
        project = _NullProject()

    assert load_paused_names(_NullConfig()) == set()


def test_is_paused_true_when_name_listed(
    config_with_base_dir: _FakeConfig,
) -> None:
    from pollypm.session_paused import is_paused

    _write_marker(config_with_base_dir, ["operator"])
    assert is_paused(config_with_base_dir, "operator") is True


def test_is_paused_false_when_name_not_listed(
    config_with_base_dir: _FakeConfig,
) -> None:
    from pollypm.session_paused import is_paused

    _write_marker(config_with_base_dir, ["operator"])
    assert is_paused(config_with_base_dir, "reviewer") is False


def test_is_paused_false_when_marker_missing(
    config_with_base_dir: _FakeConfig,
) -> None:
    from pollypm.session_paused import is_paused

    assert is_paused(config_with_base_dir, "operator") is False


def test_skip_if_paused_returns_false_for_unpaused_session(
    config_with_base_dir: _FakeConfig,
) -> None:
    from pollypm.session_paused import skip_if_paused

    events: list[tuple] = []

    class _Store:
        def record_event(self, *args, **kwargs):  # noqa: ANN002,ANN003
            events.append((args, kwargs))

    # No marker → no skip, no audit emission.
    assert skip_if_paused(
        config_with_base_dir, "operator", store=_Store(),
        loop="unit_test",
    ) is False
    assert events == []


def test_skip_if_paused_emits_audit_event_on_skip(
    config_with_base_dir: _FakeConfig,
) -> None:
    """The audit event lets the cockpit show that the loop honoured the
    pause — without it, a skipped recovery is invisible."""
    from pollypm.session_paused import (
        PAUSE_SKIP_EVENT_TYPE,
        skip_if_paused,
    )

    _write_marker(config_with_base_dir, ["operator"])

    captured: list[dict[str, Any]] = []

    class _Store:
        def record_event(self, **kwargs):  # noqa: ANN003
            captured.append(kwargs)

    assert skip_if_paused(
        config_with_base_dir, "operator", store=_Store(),
        loop="unit_test", reason="failure_type=missing_window",
    ) is True
    assert len(captured) == 1
    event = captured[0]
    assert event["sender"] == PAUSE_SKIP_EVENT_TYPE
    assert event["scope"] == "operator"
    assert "operator" in event["subject"]
    assert "unit_test" in event["subject"]
    assert event["payload"]["loop"] == "unit_test"
    assert event["payload"]["reason"] == "failure_type=missing_window"


def test_skip_if_paused_no_store_still_returns_true(
    config_with_base_dir: _FakeConfig,
) -> None:
    """Audit emission is optional — pure-boolean guard mode must work."""
    from pollypm.session_paused import skip_if_paused

    _write_marker(config_with_base_dir, ["operator"])
    # No ``store`` kwarg → no emission, but the guard still trips.
    assert skip_if_paused(
        config_with_base_dir, "operator", loop="boolean_only",
    ) is True


def test_skip_if_paused_swallows_store_errors(
    config_with_base_dir: _FakeConfig,
) -> None:
    """A flaky audit-store write must not break the guard contract.

    The recovery loops use ``skip_if_paused`` as a hard yield gate. If
    the audit emission raises (e.g. a transient DB outage), the boolean
    return MUST still be True — otherwise the loop would re-enter the
    paused session and fire the very intervention we just chose to
    skip.
    """
    from pollypm.session_paused import skip_if_paused

    _write_marker(config_with_base_dir, ["operator"])

    class _FlakyStore:
        def record_event(self, **kwargs):  # noqa: ANN003
            raise RuntimeError("simulated transient store outage")

    assert skip_if_paused(
        config_with_base_dir, "operator", store=_FlakyStore(),
        loop="flaky_store",
    ) is True


# ---------------------------------------------------------------------------
# Integration — loop 1: auto_recover_no_session_alerts
# ---------------------------------------------------------------------------


@dataclass
class _FakeAlert:
    session_name: str
    alert_type: str
    severity: str
    message: str
    status: str
    created_at: str
    updated_at: str
    alert_id: int | None = None


@dataclass
class _FakeEvent:
    session_name: str
    event_type: str
    message: str
    created_at: str


@dataclass
class _FakeStore:
    """Minimal store double covering the contract auto_recover + the
    pause-skip audit emit hit. ``record_event`` accepts BOTH positional
    (legacy) and keyword (unified) shapes so we exercise the same
    fallback the production helpers walk through."""

    alerts: list[_FakeAlert] = field(default_factory=list)
    events: list[_FakeEvent] = field(default_factory=list)
    upserted: list[tuple[str, str, str, str]] = field(default_factory=list)
    cleared: list[tuple[str, str]] = field(default_factory=list)
    kw_events: list[dict[str, Any]] = field(default_factory=list)

    def open_alerts(self) -> list[_FakeAlert]:
        return [a for a in self.alerts if a.status == "open"]

    def record_event(self, *args, **kwargs):  # noqa: ANN002,ANN003
        if kwargs:
            self.kw_events.append(kwargs)
            return
        session_name, event_type, message = args
        self.events.append(
            _FakeEvent(
                session_name=session_name,
                event_type=event_type,
                message=message,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        )

    def recent_events(self, limit: int = 20) -> list[_FakeEvent]:
        return list(reversed(self.events))[:limit]

    def upsert_alert(
        self, session_name: str, alert_type: str, severity: str, message: str,
    ) -> None:
        self.upserted.append((session_name, alert_type, severity, message))

    def clear_alert(self, session_name: str, alert_type: str, **_) -> None:
        self.cleared.append((session_name, alert_type))


@dataclass
class _FakeProjectKey:
    key: str


@dataclass
class _FakeServices:
    msg_store: _FakeStore
    state_store: _FakeStore | None = None
    known_projects: tuple[Any, ...] = ()
    config: Any = None


def _make_no_session_alert(
    *,
    session_name: str = "reviewer",
    role: str = "reviewer",
    project: str = "demo",
    age_seconds: int = 120,
) -> _FakeAlert:
    now = datetime.now(timezone.utc)
    created = (now - timedelta(seconds=age_seconds)).isoformat()
    return _FakeAlert(
        session_name=session_name,
        alert_type="no_session",
        severity="warn",
        message=(
            f"No worker is running for the {role} role on '{project}' — "
            f"task {project}/8 is stuck in the queue."
        ),
        status="open",
        created_at=created,
        updated_at=created,
    )


def test_auto_recover_no_session_skips_paused_session(
    config_with_base_dir: _FakeConfig,
) -> None:
    """Loop 1 — ``auto_recover_no_session_alerts`` consults the marker.

    The alert keys on a session named ``reviewer``; pausing that name
    via the sessions-admin marker should yield BEFORE the spawn call
    fires. This is the per-issue requirement: the marker is no longer
    informational-only for the auto-spawn loop.
    """
    from pollypm.recovery.no_session_spawn import (
        auto_recover_no_session_alerts,
    )

    _write_marker(config_with_base_dir, ["reviewer"])
    store = _FakeStore(alerts=[_make_no_session_alert()])
    services = _FakeServices(
        msg_store=store,
        known_projects=(_FakeProjectKey("demo"),),
        config=config_with_base_dir,
    )
    spawn_calls: list[tuple[str, str]] = []

    def fake_spawn(*, config_path: Path, project: str, role: str):
        spawn_calls.append((role, project))
        return True, "stub"

    decisions = auto_recover_no_session_alerts(
        services, config_path=Path("/tmp/cfg.toml"), spawn=fake_spawn,
    )
    # No spawn happened.
    assert spawn_calls == []
    # The outcome label is the new ``skipped_paused``.
    assert [d.outcome for d in decisions] == ["skipped_paused"]
    # The audit event landed on the unified-keyword path.
    assert any(
        ev.get("sender") == "session.pause.skip" for ev in store.kw_events
    ), store.kw_events


def test_auto_recover_no_session_spawns_when_not_paused(
    config_with_base_dir: _FakeConfig,
) -> None:
    """Control: with no marker, the spawn still fires — guard is opt-in."""
    from pollypm.recovery.no_session_spawn import (
        auto_recover_no_session_alerts,
    )

    store = _FakeStore(alerts=[_make_no_session_alert()])
    services = _FakeServices(
        msg_store=store,
        known_projects=(_FakeProjectKey("demo"),),
        config=config_with_base_dir,  # base_dir present but no marker
    )
    spawn_calls: list[tuple[str, str]] = []

    def fake_spawn(*, config_path: Path, project: str, role: str):
        spawn_calls.append((role, project))
        return True, "stub"

    decisions = auto_recover_no_session_alerts(
        services, config_path=Path("/tmp/cfg.toml"), spawn=fake_spawn,
    )
    assert spawn_calls == [("reviewer", "demo")]
    assert [d.outcome for d in decisions] == ["spawned"]


def test_auto_recover_no_session_honours_project_scoped_pause_name(
    config_with_base_dir: _FakeConfig,
) -> None:
    """The pause check covers BOTH the alert's session_name AND the
    expected per-project session expansion — pausing either spelling
    should yield."""
    from pollypm.recovery.no_session_spawn import (
        _expected_session_name,
        auto_recover_no_session_alerts,
    )

    # Pause the expected-session expansion, NOT the alert's session_name.
    expected = _expected_session_name("reviewer", "demo")
    assert expected  # sanity — the helper returned something
    _write_marker(config_with_base_dir, [expected])

    store = _FakeStore(alerts=[_make_no_session_alert()])
    services = _FakeServices(
        msg_store=store,
        known_projects=(_FakeProjectKey("demo"),),
        config=config_with_base_dir,
    )
    spawn_calls: list[tuple[str, str]] = []

    def fake_spawn(*, config_path: Path, project: str, role: str):
        spawn_calls.append((role, project))
        return True, "stub"

    decisions = auto_recover_no_session_alerts(
        services, config_path=Path("/tmp/cfg.toml"), spawn=fake_spawn,
    )
    # If `expected == "reviewer"` (the alert's own session_name) this
    # collapses to the basic case. Either way: no spawn.
    assert spawn_calls == []
    assert [d.outcome for d in decisions] == ["skipped_paused"]


# ---------------------------------------------------------------------------
# Integration — loops 2 & 3: Supervisor.maybe_recover_session
# ---------------------------------------------------------------------------


def test_supervisor_maybe_recover_session_skips_paused_session(
    config_with_base_dir: _FakeConfig,
) -> None:
    """Loops 2 & 3 — the supervisor's recovery chokepoint yields.

    ``Supervisor.maybe_recover_session`` is the single apply path the
    periodic health sweep (loop 3) AND the policy intervention path
    (loop 2 — ``DefaultRecoveryPolicy``'s recommendation consumer) both
    route through. Adding the guard here covers both loops without
    threading the marker into the policy class itself (which is sealed
    to stay pure per ``pollypm.recovery.base.RecoveryPolicy``).
    """
    from pollypm.supervisor import Supervisor

    _write_marker(config_with_base_dir, ["operator"])

    # A SessionLaunchSpec-shaped double — we never call into the real
    # one because the guard returns BEFORE any planner / tmux work.
    @dataclass
    class _FakeSession:
        name: str

    @dataclass
    class _FakeLaunch:
        session: _FakeSession
        window_name: str = "op"

    launch = _FakeLaunch(session=_FakeSession(name="operator"))

    # Build a Supervisor stub that only carries the two attributes the
    # guard reads. We DELIBERATELY skip ``Supervisor.__init__`` (which
    # opens sqlite + plugin host) by using ``__new__`` — the guard runs
    # before any of those collaborators are touched.
    sup = Supervisor.__new__(Supervisor)
    sup.config = config_with_base_dir

    captured: list[dict[str, Any]] = []

    class _MsgStore:
        def record_event(self, **kwargs):  # noqa: ANN003
            captured.append(kwargs)

        # Methods the apply path would call AFTER the guard — none
        # should fire. We attach asserting stubs so a regression that
        # drops the guard is loud.
        def append_event(self, **kwargs):  # noqa: ANN003
            raise AssertionError(
                "append_event called after paused-skip guard should "
                f"have yielded: {kwargs}"
            )

        def upsert_alert(self, *args, **kwargs):  # noqa: ANN002,ANN003
            raise AssertionError(
                "upsert_alert called after paused-skip guard should "
                f"have yielded: {args} {kwargs}"
            )

        def clear_alert(self, *args, **kwargs):  # noqa: ANN002,ANN003
            raise AssertionError("clear_alert called after pause guard")

    sup._msg_store = _MsgStore()

    # Invoke. Should return cleanly with NO side effects beyond the
    # audit event.
    sup.maybe_recover_session(
        launch, failure_type="missing_window",
        failure_message="window missing",
    )

    assert len(captured) == 1, captured
    event = captured[0]
    assert event["sender"] == "session.pause.skip"
    assert event["scope"] == "operator"
    assert "supervisor.maybe_recover_session" in event["subject"]
    assert event["payload"]["loop"] == "supervisor.maybe_recover_session"
    assert "missing_window" in event["payload"]["reason"]


def test_supervisor_maybe_recover_session_proceeds_when_not_paused(
    config_with_base_dir: _FakeConfig,
) -> None:
    """Control: without a marker, the guard yields False and the
    existing apply path runs. We assert by observing that the policy
    recommendation lookup is reached (it raises in our stub setup;
    proving the guard didn't short-circuit).
    """
    from pollypm.supervisor import Supervisor

    # NO marker written → guard returns False.

    @dataclass
    class _FakeSession:
        name: str

    @dataclass
    class _FakeLaunch:
        session: _FakeSession
        window_name: str = "op"

    launch = _FakeLaunch(session=_FakeSession(name="operator"))

    sup = Supervisor.__new__(Supervisor)
    sup.config = config_with_base_dir

    reached_policy: list[str] = []

    class _MsgStore:
        def record_event(self, **kwargs):  # noqa: ANN003
            # Should NOT see the pause.skip event when not paused.
            assert kwargs.get("sender") != "session.pause.skip", kwargs

        def append_event(self, **kwargs):  # noqa: ANN003
            reached_policy.append(kwargs.get("subject", ""))
            # Raise after recording so the rest of the apply path
            # doesn't try to touch other collaborators we haven't
            # stubbed.
            raise _Reached("policy path entered")

    class _Reached(Exception):
        pass

    sup._msg_store = _MsgStore()

    # Force the policy lookup to return a recommendation so
    # append_event fires immediately. ``_policy_recommendation`` is the
    # first thing past the pause guard.
    class _Rec:
        action = "nudge"
        reason = "test"

    sup._policy_recommendation = lambda *_a, **_kw: _Rec()  # type: ignore[method-assign]

    class _Policy:
        name = "default"

    sup._recovery_policy = _Policy()

    with pytest.raises(_Reached):
        sup.maybe_recover_session(
            launch, failure_type="missing_window",
            failure_message="window missing",
        )
    # Confirms the apply path was entered (append_event called).
    assert reached_policy
