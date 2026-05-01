from __future__ import annotations

from pathlib import Path

import pollypm.supervisor_alerts as _supervisor_alerts
from pollypm.models import (
    AccountConfig,
    KnownProject,
    PollyPMConfig,
    PollyPMSettings,
    ProjectKind,
    ProjectSettings,
    ProviderKind,
    SessionConfig,
)
from pollypm.supervisor import Supervisor
from pollypm.tmux.client import TmuxWindow


def _config(tmp_path: Path) -> PollyPMConfig:
    return PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm/logs",
            snapshots_dir=tmp_path / ".pollypm/snapshots",
            state_db=tmp_path / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(
            controller_account="claude_controller",
            failover_enabled=True,
            failover_accounts=["codex_backup"],
        ),
        accounts={
            "claude_controller": AccountConfig(
                name="claude_controller",
                provider=ProviderKind.CLAUDE,
                email="claude@example.com",
                home=tmp_path / ".pollypm/homes/claude_controller",
            ),
            "codex_backup": AccountConfig(
                name="codex_backup",
                provider=ProviderKind.CODEX,
                email="codex@example.com",
                home=tmp_path / ".pollypm/homes/codex_backup",
            ),
        },
        sessions={
            "worker": SessionConfig(
                name="worker",
                role="worker",
                provider=ProviderKind.CODEX,
                account="codex_backup",
                cwd=tmp_path,
                project="pollypm",
                prompt="Ship the fix",
                window_name="worker-pollypm",
            ),
        },
        projects={
            "pollypm": KnownProject(
                key="pollypm",
                path=tmp_path,
                name="PollyPM",
                kind=ProjectKind.FOLDER,
            )
        },
    )


def test_supervisor_alert_helper_updates_and_nudges(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    launch = next(item for item in supervisor.plan_launches() if item.session.name == "worker")
    window = TmuxWindow(
        session=supervisor.storage_closet_session_name(),
        index=1,
        name="worker-pollypm",
        active=False,
        pane_id="%42",
        pane_current_command="codex",
        pane_current_path=str(tmp_path),
        pane_dead=False,
    )

    for index in range(4):
        supervisor.store.record_heartbeat(
            session_name="worker",
            tmux_window=window.name,
            pane_id=window.pane_id,
            pane_command=window.pane_current_command,
            pane_dead=False,
            log_bytes=100 + index,
            snapshot_path=str(tmp_path / f"snapshot-{index}.txt"),
            snapshot_hash="same-hash",
        )
    supervisor.store.record_heartbeat(
        session_name="worker",
        tmux_window=window.name,
        pane_id=window.pane_id,
        pane_command=window.pane_current_command,
        pane_dead=False,
        log_bytes=200,
        snapshot_path=str(tmp_path / "snapshot-current.txt"),
        snapshot_hash="same-hash",
    )

    sent: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(
        supervisor,
        "send_input",
        lambda session_name, text, owner="pollypm", force=False, press_enter=True: sent.append(
            (session_name, text, force)
        ),
    )

    # #765 classifier gates suspected_loop on has_pending_work — simulate
    # a queued task so this stall-detection test stays focused on the
    # nudge path rather than needing a seeded work-service DB.
    monkeypatch.setattr(
        "pollypm.heartbeats.stall_classifier.has_pending_work_for_session",
        lambda config, session_name: True,
    )

    alerts = _supervisor_alerts._update_alerts(
        supervisor,
        launch,
        window,
        pane_text="Still stalled",
        previous_log_bytes=150,
        previous_snapshot_hash="same-hash",
        current_log_bytes=200,
        current_snapshot_hash="same-hash",
    )

    assert "suspected_loop" in alerts
    assert sent == [("worker", Supervisor._STALL_NUDGE_MESSAGE, False)]


def test_supervisor_wrapper_delegates_alert_helper(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    launch = next(item for item in supervisor.plan_launches() if item.session.name == "worker")
    window = TmuxWindow(
        session=supervisor.storage_closet_session_name(),
        index=1,
        name="worker-pollypm",
        active=False,
        pane_id="%42",
        pane_current_command="codex",
        pane_current_path=str(tmp_path),
        pane_dead=False,
    )

    monkeypatch.setattr(
        _supervisor_alerts,
        "_update_alerts",
        lambda *args, **kwargs: ["delegated"],
    )

    alerts = supervisor._update_alerts(
        launch,
        window,
        pane_text="ignore",
        previous_log_bytes=None,
        previous_snapshot_hash=None,
        current_log_bytes=1,
        current_snapshot_hash="hash",
    )

    assert alerts == ["delegated"]


# ---------------------------------------------------------------------------
# #910 follow-up — record_event sites route through SignalEnvelope
# ---------------------------------------------------------------------------


class _FakeMsgStore:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def record_event(self, *, scope: str, sender: str, subject: str, payload: dict) -> int:
        self.events.append(
            {"scope": scope, "sender": sender, "subject": subject, "payload": payload}
        )
        return len(self.events)


class _FakeSupervisorBoundary:
    """Minimal stand-in for SupervisorAlertBoundary suitable for the
    record_event-routing test. Only ``msg_store`` is exercised by the
    funnel under test."""

    def __init__(self) -> None:
        self.msg_store = _FakeMsgStore()


def test_supervisor_alerts_emit_routed_event_routes_through_signal_envelope(
    monkeypatch,
) -> None:
    """#910 follow-up — every event written through
    ``_emit_routed_event`` must construct a SignalEnvelope and pass
    it through ``route_signal`` BEFORE the legacy
    ``msg_store.record_event`` write.

    Patches the funnel's ``_route_signal`` reference with a recording
    stub so the test can read back the envelope and confirm:
      * the envelope was built and routed (call count == 1),
      * the legacy persistence still ran exactly once,
      * the envelope carries OPERATIONAL actionability + OPERATOR
        audience (so the routing policy lands it on Activity only),
      * the dedupe key names the source + subject + scope, matching
        the convention used by the heartbeat-side funnel.
    """
    from pollypm.signal_routing import (
        SignalActionability,
        SignalAudience,
    )

    captured: list = []

    def _record(envelope):
        captured.append(envelope)
        return None  # route_signal return value unused by funnel

    monkeypatch.setattr(_supervisor_alerts, "_route_signal", _record)

    boundary = _FakeSupervisorBoundary()
    _supervisor_alerts._emit_routed_event(
        boundary,
        scope="worker",
        sender="worker",
        subject="heartbeat_nudge_skipped",
        payload={"message": "Skipped"},
    )

    assert len(captured) == 1
    env = captured[0]
    assert env.source == "supervisor_alerts"
    assert env.subject == "heartbeat_nudge_skipped"
    assert env.audience is SignalAudience.OPERATOR
    assert env.actionability is SignalActionability.OPERATIONAL
    assert env.dedupe_key is not None
    assert "supervisor_alerts" in env.dedupe_key
    assert "heartbeat_nudge_skipped" in env.dedupe_key
    assert "worker" in env.dedupe_key
    assert env.body == "Skipped"

    # Legacy persistence still happens — the funnel preserves the
    # event-store write so existing readers don't regress.
    assert len(boundary.msg_store.events) == 1
    persisted = boundary.msg_store.events[0]
    assert persisted["subject"] == "heartbeat_nudge_skipped"
    assert persisted["scope"] == "worker"


# ---------------------------------------------------------------------------
# #1008 — recovery_limit / stuck_session auto-clear after healthy streak
# ---------------------------------------------------------------------------


def _backdate_alert(
    supervisor: Supervisor, session_name: str, alert_type: str, seconds_ago: float,
) -> None:
    """Backdate an open alert's ``created_at`` / ``updated_at`` so the
    streak math has runway.

    ``upsert_alert`` timestamps with ``now()`` and ``Store.update_message``
    auto-stamps ``updated_at = now()`` on every patch — so to push the
    timestamps into the past we reach through the engine directly, the
    same pattern used by other timestamp-sensitive tests in the suite.
    """
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import update

    from pollypm.store.schema import messages

    target = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    engine = supervisor._msg_store._write_engine  # type: ignore[attr-defined]
    with engine.begin() as conn:
        conn.execute(
            update(messages)
            .where(messages.c.scope == session_name)
            .where(messages.c.sender == alert_type)
            .where(messages.c.state == "open")
            .values(created_at=target, updated_at=target)
        )


def test_recovery_alert_auto_clear_clears_recovery_limit_after_debounce(
    monkeypatch, tmp_path: Path,
) -> None:
    """Healthy session + open ``recovery_limit`` alert older than the
    debounce window → the heartbeat sweep clears it and resets the
    recovery counter.
    """
    from datetime import UTC, datetime, timedelta

    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    launch = next(item for item in supervisor.plan_launches() if item.session.name == "worker")

    # Raise a recovery_limit alert and a recovery_attempts counter.
    supervisor._msg_store.upsert_alert(
        launch.session.name, "recovery_limit", "error",
        "Automatic recovery paused after 5 rapid failures",
    )
    supervisor.store.upsert_session_runtime(
        session_name=launch.session.name,
        status="degraded",
        recovery_attempts=5,
        recovery_window_started_at=datetime.now(UTC).isoformat(),
    )
    # Backdate the alert past the debounce window.
    _backdate_alert(supervisor, launch.session.name, "recovery_limit", 200)

    # Stub the window state — pretend the session's expected window is
    # alive and the launch plan still includes it. The sweep reads
    # ``window_map`` (window_name → TmuxWindow) and ``name_by_window``
    # (window_name → session_name) directly, so we can hand it
    # synthetic dicts without touching tmux.
    window_map = {launch.window_name: object()}
    name_by_window = {launch.window_name: launch.session.name}

    supervisor._sweep_recovered_recovery_alerts(
        window_map=window_map, name_by_window=name_by_window,
    )

    open_pairs = [
        (a.session_name, a.alert_type) for a in supervisor.open_alerts()
    ]
    assert (launch.session.name, "recovery_limit") not in open_pairs, (
        f"recovery_limit must auto-clear after debounce; saw {open_pairs!r}"
    )
    runtime = supervisor.store.get_session_runtime(launch.session.name)
    assert runtime is not None
    assert runtime.recovery_attempts == 0, (
        "recovery_attempts must reset after auto-clear so a subsequent "
        "failure gets the full retry budget again"
    )
    assert runtime.recovery_window_started_at is None


def test_recovery_alert_auto_clear_clears_stuck_session_after_debounce(
    monkeypatch, tmp_path: Path,
) -> None:
    """``stuck_session`` is on the same auto-clear path — covered separately
    so a regression that only handles ``recovery_limit`` doesn't slip
    through.
    """
    from datetime import UTC, datetime, timedelta

    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    launch = next(item for item in supervisor.plan_launches() if item.session.name == "worker")

    supervisor._msg_store.upsert_alert(
        launch.session.name, "stuck_session", "warn",
        f"{launch.session.name} needs attention: persistently idle",
    )
    _backdate_alert(supervisor, launch.session.name, "stuck_session", 200)

    window_map = {launch.window_name: object()}
    name_by_window = {launch.window_name: launch.session.name}

    supervisor._sweep_recovered_recovery_alerts(
        window_map=window_map, name_by_window=name_by_window,
    )

    open_pairs = [
        (a.session_name, a.alert_type) for a in supervisor.open_alerts()
    ]
    assert (launch.session.name, "stuck_session") not in open_pairs


def test_recovery_alert_auto_clear_holds_alert_within_debounce(
    monkeypatch, tmp_path: Path,
) -> None:
    """Session healthy this tick but the alert is younger than the
    debounce window → leave the alert open. Prevents clearing during a
    flap.
    """
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    launch = next(item for item in supervisor.plan_launches() if item.session.name == "worker")

    # Default debounce is 90s — the just-raised alert is well inside it.
    supervisor._msg_store.upsert_alert(
        launch.session.name, "recovery_limit", "error",
        "Automatic recovery paused after 5 rapid failures",
    )

    window_map = {launch.window_name: object()}
    name_by_window = {launch.window_name: launch.session.name}

    supervisor._sweep_recovered_recovery_alerts(
        window_map=window_map, name_by_window=name_by_window,
    )

    open_pairs = [
        (a.session_name, a.alert_type) for a in supervisor.open_alerts()
    ]
    assert (launch.session.name, "recovery_limit") in open_pairs, (
        "fresh alerts must NOT auto-clear during the debounce window"
    )


def test_recovery_alert_auto_clear_resets_streak_when_window_missing(
    monkeypatch, tmp_path: Path,
) -> None:
    """If the session's window is gone *during* the streak, the next
    healthy tick must restart the debounce — a single unhealthy tick in
    the middle of an otherwise-long streak should NOT trigger a clear.
    """
    from datetime import UTC, datetime, timedelta

    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    launch = next(item for item in supervisor.plan_launches() if item.session.name == "worker")

    supervisor._msg_store.upsert_alert(
        launch.session.name, "recovery_limit", "error",
        "Automatic recovery paused after 5 rapid failures",
    )
    _backdate_alert(supervisor, launch.session.name, "recovery_limit", 200)

    # Tick 1: window MISSING → unhealthy observation recorded.
    window_map_missing: dict = {}
    name_by_window = {launch.window_name: launch.session.name}
    supervisor._sweep_recovered_recovery_alerts(
        window_map=window_map_missing, name_by_window=name_by_window,
    )

    # Tick 2: window BACK + immediately past debounce on the alert
    # timestamp — but the unhealthy observation in tick 1 should
    # restart the streak, so the alert must remain open.
    window_map = {launch.window_name: object()}
    supervisor._sweep_recovered_recovery_alerts(
        window_map=window_map, name_by_window=name_by_window,
    )

    open_pairs = [
        (a.session_name, a.alert_type) for a in supervisor.open_alerts()
    ]
    assert (launch.session.name, "recovery_limit") in open_pairs, (
        "an unhealthy tick mid-streak must reset the debounce"
    )


def test_recovery_alert_auto_clear_skips_untracked_sessions(
    monkeypatch, tmp_path: Path,
) -> None:
    """A ``recovery_limit`` whose session isn't in the launch plan is
    handled by ``_sweep_stale_alerts``, not the auto-clear path. Skip
    so we don't fight the orphan policy.
    """
    from datetime import UTC, datetime, timedelta

    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    # Untracked session name — not in ``config.sessions``.
    supervisor._msg_store.upsert_alert(
        "ghost-session", "recovery_limit", "error",
        "Automatic recovery paused after 5 rapid failures",
    )
    _backdate_alert(supervisor, "ghost-session", "recovery_limit", 200)

    window_map: dict = {}
    name_by_window: dict = {}
    supervisor._sweep_recovered_recovery_alerts(
        window_map=window_map, name_by_window=name_by_window,
    )

    open_pairs = [
        (a.session_name, a.alert_type) for a in supervisor.open_alerts()
    ]
    assert ("ghost-session", "recovery_limit") in open_pairs, (
        "auto-clear sweep must not touch untracked sessions; "
        "_sweep_stale_alerts owns that path"
    )


def test_recovery_alert_auto_clear_full_lifecycle(
    monkeypatch, tmp_path: Path,
) -> None:
    """End-to-end: raise alert, observe healthy across tick, advance
    past debounce, alert clears. Mirrors the issue #1008 acceptance
    flow (raise → confirm healthy → tick past debounce → cleared).
    """
    from datetime import UTC, datetime, timedelta

    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    launch = next(item for item in supervisor.plan_launches() if item.session.name == "worker")

    # Step (a) — raise recovery_limit, just like the supervisor would
    # after exhausting the auto-recovery budget.
    supervisor._msg_store.upsert_alert(
        launch.session.name, "recovery_limit", "error",
        "Automatic recovery paused after 5 rapid failures",
    )
    open_pairs = {
        (a.session_name, a.alert_type) for a in supervisor.open_alerts()
    }
    assert (launch.session.name, "recovery_limit") in open_pairs

    # Step (b) — first healthy tick BEFORE debounce → alert holds.
    window_map = {launch.window_name: object()}
    name_by_window = {launch.window_name: launch.session.name}
    supervisor._sweep_recovered_recovery_alerts(
        window_map=window_map, name_by_window=name_by_window,
    )
    open_pairs = {
        (a.session_name, a.alert_type) for a in supervisor.open_alerts()
    }
    assert (launch.session.name, "recovery_limit") in open_pairs

    # Step (c) — advance the alert timestamp past the debounce, observe
    # again → alert clears.
    _backdate_alert(
        supervisor, launch.session.name, "recovery_limit",
        Supervisor._RECOVERY_ALERT_AUTO_CLEAR_DEBOUNCE_SECONDS + 30,
    )
    supervisor._sweep_recovered_recovery_alerts(
        window_map=window_map, name_by_window=name_by_window,
    )

    open_pairs = {
        (a.session_name, a.alert_type) for a in supervisor.open_alerts()
    }
    assert (launch.session.name, "recovery_limit") not in open_pairs


def test_supervisor_alerts_heartbeat_nudge_skipped_path_routes_through_funnel(
    monkeypatch, tmp_path: Path,
) -> None:
    """#910 follow-up — the human-leased worker nudge-skip site
    (formerly a raw ``msg_store.record_event`` call) now goes through
    ``_emit_routed_event``. Exercising the public
    ``_maybe_nudge_stalled_session`` entry point asserts the funnel
    runs end-to-end on the legacy code path."""
    from pollypm.signal_routing import SignalActionability

    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    launch = next(
        item for item in supervisor.plan_launches() if item.session.name == "worker"
    )

    # Force the human-leased branch in _maybe_nudge_stalled_session.
    supervisor.store.set_lease(launch.session.name, "human")

    captured: list = []

    def _record(envelope):
        captured.append(envelope)
        return None

    monkeypatch.setattr(_supervisor_alerts, "_route_signal", _record)

    _supervisor_alerts._maybe_nudge_stalled_session(supervisor, launch)

    matching = [
        env for env in captured
        if env.dedupe_key and "heartbeat_nudge_skipped" in env.dedupe_key
    ]
    assert matching, [env.dedupe_key for env in captured]
    env = matching[0]
    assert env.source == "supervisor_alerts"
    assert env.actionability is SignalActionability.OPERATIONAL
