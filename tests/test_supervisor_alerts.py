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


# ---------------------------------------------------------------------------
# #1028 — no_session_spawn_failed auto-clear + breaker reset on heartbeat success
# ---------------------------------------------------------------------------


def test_recovery_alert_auto_clear_clears_no_session_spawn_failed_after_debounce(
    monkeypatch, tmp_path: Path,
) -> None:
    """Healthy session + open ``no_session_spawn_failed`` alert older
    than the debounce window → the heartbeat sweep clears it AND records
    a breaker-reset marker so the next ``no_session`` episode starts
    with a fresh attempt budget rather than the tripped state.
    """
    from pollypm.recovery.no_session_spawn import (
        SPAWN_ATTEMPT_EVENT_TYPE,
        SPAWN_BREAKER_RESET_EVENT_TYPE,
        _attempt_history,
        _latest_breaker_reset,
    )

    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    launch = next(
        item for item in supervisor.plan_launches()
        if item.session.name == "worker"
    )

    # Trip the breaker — record 3 prior spawn attempts (backdated 5
    # minutes so they predate the reset marker by a clear margin; the
    # store's created_at column is second-precision and a same-second
    # reset+attempt would not be cleanly separable) and raise the
    # spawn_failed escalation alert just like
    # ``no_session_spawn._escalate_failure`` would.
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import update

    from pollypm.store.schema import messages

    for index in range(3):
        supervisor._msg_store.record_event(
            scope=launch.session.name,
            sender=SPAWN_ATTEMPT_EVENT_TYPE,
            subject=f"attempt {index + 1}",
        )
    # Backdate the spawn-attempt rows so the post-reset filter has clear
    # second-precision separation.
    backdated = datetime.now(timezone.utc) - timedelta(seconds=300)
    engine = supervisor._msg_store._write_engine  # type: ignore[attr-defined]
    with engine.begin() as conn:
        conn.execute(
            update(messages)
            .where(messages.c.scope == launch.session.name)
            .where(messages.c.sender == SPAWN_ATTEMPT_EVENT_TYPE)
            .values(created_at=backdated, updated_at=backdated)
        )
    supervisor._msg_store.upsert_alert(
        launch.session.name, "no_session_spawn_failed", "error",
        f"Auto-recovery for worker/{launch.session.project} failed after "
        "3 spawn attempts. Run `pm worker-start --role worker "
        f"{launch.session.project}` manually and check logs for the "
        "underlying error.",
    )
    # Sanity: history reads as 3 (no reset yet).
    assert len(
        _attempt_history(supervisor._msg_store, launch.session.name)
    ) == 3
    # Backdate the alert past the debounce window so the sweep clears it.
    _backdate_alert(
        supervisor, launch.session.name, "no_session_spawn_failed", 200,
    )

    window_map = {launch.window_name: object()}
    name_by_window = {launch.window_name: launch.session.name}

    supervisor._sweep_recovered_recovery_alerts(
        window_map=window_map, name_by_window=name_by_window,
    )

    open_pairs = [
        (a.session_name, a.alert_type) for a in supervisor.open_alerts()
    ]
    assert (
        launch.session.name, "no_session_spawn_failed",
    ) not in open_pairs, (
        "no_session_spawn_failed must auto-clear after debounce; "
        f"saw {open_pairs!r}"
    )

    # The breaker-reset marker is now the floor for ``_attempt_history``,
    # so the historical attempts no longer count → the next episode has
    # the full budget.
    reset_ts = _latest_breaker_reset(
        supervisor._msg_store, launch.session.name,
    )
    assert reset_ts is not None, (
        f"breaker reset marker ({SPAWN_BREAKER_RESET_EVENT_TYPE}) must be "
        "recorded when the auto-clear sweep closes a "
        "no_session_spawn_failed alert"
    )
    assert _attempt_history(
        supervisor._msg_store, launch.session.name,
    ) == [], (
        "spawn-attempt counter must reset to zero after auto-clear so a "
        "subsequent failure gets the full retry budget"
    )


def test_recovery_alert_auto_clear_clears_spawn_failed_persistent(
    monkeypatch, tmp_path: Path,
) -> None:
    """The #1014 ``spawn_failed_persistent`` variant is on the same
    auto-clear path. Covered separately so a regression that only handles
    ``no_session_spawn_failed`` can't slip through.
    """
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    launch = next(
        item for item in supervisor.plan_launches()
        if item.session.name == "worker"
    )

    supervisor._msg_store.upsert_alert(
        launch.session.name, "spawn_failed_persistent", "error",
        "Auto-spawn for task pollypm/1 has failed 5 times in a row",
    )
    _backdate_alert(
        supervisor, launch.session.name, "spawn_failed_persistent", 200,
    )

    window_map = {launch.window_name: object()}
    name_by_window = {launch.window_name: launch.session.name}

    supervisor._sweep_recovered_recovery_alerts(
        window_map=window_map, name_by_window=name_by_window,
    )

    open_pairs = [
        (a.session_name, a.alert_type) for a in supervisor.open_alerts()
    ]
    assert (
        launch.session.name, "spawn_failed_persistent",
    ) not in open_pairs


def test_recovery_alert_auto_clear_holds_spawn_failed_within_debounce(
    monkeypatch, tmp_path: Path,
) -> None:
    """A just-raised ``no_session_spawn_failed`` must NOT clear during
    the debounce window — protect against clearing during a spawn flap.
    """
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    launch = next(
        item for item in supervisor.plan_launches()
        if item.session.name == "worker"
    )

    supervisor._msg_store.upsert_alert(
        launch.session.name, "no_session_spawn_failed", "error",
        "Auto-recovery failed after 3 spawn attempts",
    )

    window_map = {launch.window_name: object()}
    name_by_window = {launch.window_name: launch.session.name}

    supervisor._sweep_recovered_recovery_alerts(
        window_map=window_map, name_by_window=name_by_window,
    )

    open_pairs = [
        (a.session_name, a.alert_type) for a in supervisor.open_alerts()
    ]
    assert (
        launch.session.name, "no_session_spawn_failed",
    ) in open_pairs, (
        "fresh spawn_failed alerts must NOT auto-clear during the "
        "debounce window"
    )


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


# ---------------------------------------------------------------------------
# #1010 — idle-placeholder detection in the session-health classifier
# ---------------------------------------------------------------------------


def test_codex_idle_placeholder_pane_classifies_as_legitimate_idle() -> None:
    """A Codex pane sitting at the rotating ``› <suggestion>`` placeholder
    must classify as ``legitimate_idle`` even when the worker has pending
    work — that's the alive-but-idle case (#1010), NOT a stall.
    """
    from pollypm.heartbeats.stall_classifier import StallContext, classify_stall
    from pollypm.idle_placeholders import pane_is_idle_placeholder

    pane_text = "\n".join([
        "• Ran pm task next -p booktalk",
        "  └ No tasks available.",
        "",
        "──────────────────────────────────────────────────────",
        "",
        "› Improve documentation in @filename",
        "",
        "  gpt-5.4 default · ~/dev/booktalk/.pollypm/worktrees/worker_booktalk",
    ])

    assert pane_is_idle_placeholder(pane_text) is True

    ctx = StallContext(
        role="worker",
        session_name="worker_booktalk",
        has_pending_work=True,  # Worker has queued work.
        pane_is_idle_placeholder=True,
    )
    assert classify_stall(ctx) == "legitimate_idle"


def test_claude_empty_prompt_pane_classifies_as_legitimate_idle() -> None:
    """The operator (Claude CLI) sitting at an empty ``❯`` prompt with no
    recent assistant output is the same alive-but-idle case as the Codex
    placeholder — not a stall (#1010 — operator/recovery_limit was one of
    the live alerts that needed this fix to auto-clear).
    """
    from pollypm.heartbeats.stall_classifier import StallContext, classify_stall
    from pollypm.idle_placeholders import pane_is_idle_placeholder

    pane_text = "\n".join([
        "  state was operator-pm.",
        "  Your previous session was interrupted and has been restarted.",
        "",
        "⏺ Batching. Standing by.",
        "",
        "✻ Cogitated for 1s",
        "",
        "──────────────────────────────────────────────────────",
        "❯ ",
        "──────────────────────────────────────────────────────",
        "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
    ])

    assert pane_is_idle_placeholder(pane_text) is True

    ctx = StallContext(
        role="operator-pm",
        session_name="operator",
        has_pending_work=False,
        pane_is_idle_placeholder=True,
    )
    assert classify_stall(ctx) == "legitimate_idle"


def test_no_tasks_available_pane_classifies_as_idle_placeholder() -> None:
    """A worker pane whose most recent reply is ``No tasks available.`` is
    an alive-but-idle worker — it ran ``pm task next``, got the canonical
    no-work response, and is correctly waiting for the queue.

    Without this short-circuit the recovery classifier treats the worker
    as ``SILENT_WORKER`` (last_event_seconds_ago > 30min, no claim),
    fires ``prompt_pm_task_next`` every cycle, and escalates to
    ``alert`` after 3 cycles even though the worker is responding
    correctly (#1084).
    """
    from pollypm.idle_placeholders import (
        pane_is_idle_placeholder,
        pane_shows_no_tasks_available,
    )

    pane_text = "\n".join([
        "› H: pm task next",
        "• Ran pm task next",
        "  └ No tasks available.",
        "",
        "──────────────────────────────────────────────────────",
        "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
    ])

    assert pane_shows_no_tasks_available(pane_text) is True
    assert pane_is_idle_placeholder(pane_text) is True


def test_no_tasks_available_does_not_match_when_followed_by_real_output() -> None:
    """The detector walks from the tail and only fires when the most
    recent informative line is ``No tasks available.``. A worker that
    received the no-work reply and then started doing other work (so the
    last line is real assistant output) must not be treated as idle.
    """
    from pollypm.idle_placeholders import pane_shows_no_tasks_available

    pane_text = "\n".join([
        "• Ran pm task next",
        "  └ No tasks available.",
        "• Picked up follow-up task work — running migrations now.",
    ])

    assert pane_shows_no_tasks_available(pane_text) is False


def test_default_recovery_policy_classifies_no_tasks_pane_as_healthy() -> None:
    """End-to-end #1084 contract: a worker whose last reply is
    ``No tasks available.`` short-circuits to ``HEALTHY`` even when the
    silent-worker timer (``last_event_seconds_ago > 30min``) would
    otherwise flag it as ``SILENT_WORKER``.
    """
    from pollypm.recovery.base import SessionHealth, SessionSignals
    from pollypm.recovery.default import DefaultRecoveryPolicy

    policy = DefaultRecoveryPolicy()

    healthy_idle = SessionSignals(
        session_name="worker_pomodoro",
        window_present=True,
        pane_dead=False,
        session_role="worker",
        last_event_seconds_ago=3600,  # Past SILENT_WORKER_SECONDS.
        pane_is_idle_placeholder=True,
    )
    assert policy.classify(healthy_idle) == SessionHealth.HEALTHY

    # Without the placeholder flag the same signals classify as
    # SILENT_WORKER, confirming the short-circuit is what flips the
    # verdict (and matches the #1084 false-positive symptom).
    silent = SessionSignals(
        session_name="worker_pomodoro",
        window_present=True,
        pane_dead=False,
        session_role="worker",
        last_event_seconds_ago=3600,
        pane_is_idle_placeholder=False,
    )
    assert policy.classify(silent) == SessionHealth.SILENT_WORKER


def test_default_recovery_policy_classifies_placeholder_pane_as_healthy(
    tmp_path: Path,
) -> None:
    """Wired end-to-end through the recovery policy: a Codex pane with
    the placeholder hint short-circuits to ``HEALTHY`` even when the
    other signals (``snapshot_repeated``, ``output_stale``) would
    otherwise classify it as STUCK / LOOPING / IDLE — preventing the
    heartbeat from re-bumping ``stuck_session`` against a placeholder
    pane and blocking #1008's auto-clear streak.
    """
    from pollypm.recovery.base import SessionHealth, SessionSignals
    from pollypm.recovery.default import DefaultRecoveryPolicy

    policy = DefaultRecoveryPolicy()

    placeholder_signals = SessionSignals(
        session_name="worker_booktalk",
        window_present=True,
        pane_dead=False,
        output_stale=True,
        snapshot_repeated=5,  # Would classify as LOOPING without the gate.
        idle_cycles=5,
        session_role="worker",
        pane_is_idle_placeholder=True,
    )
    assert policy.classify(placeholder_signals) == SessionHealth.HEALTHY

    # Without the placeholder flag the same signal stack returns LOOPING,
    # confirming the short-circuit is what flips the verdict.
    no_placeholder = SessionSignals(
        session_name="worker_booktalk",
        window_present=True,
        pane_dead=False,
        output_stale=True,
        snapshot_repeated=5,
        idle_cycles=5,
        session_role="worker",
        pane_is_idle_placeholder=False,
    )
    assert policy.classify(no_placeholder) == SessionHealth.LOOPING


def test_default_policy_placeholder_does_not_mask_real_failures(
    tmp_path: Path,
) -> None:
    """A pane carrying the idle placeholder text on top of a real
    mechanical failure (pane_dead, auth_failure, missing window) must
    still classify as the failure — the placeholder gate is for the
    alive-but-idle case, not a blanket "always healthy" override.
    """
    from pollypm.recovery.base import SessionHealth, SessionSignals
    from pollypm.recovery.default import DefaultRecoveryPolicy

    policy = DefaultRecoveryPolicy()

    # window gone → EXITED, not HEALTHY
    assert policy.classify(SessionSignals(
        session_name="x", window_present=False,
        pane_is_idle_placeholder=True,
    )) == SessionHealth.EXITED

    # pane dead → EXITED, not HEALTHY
    assert policy.classify(SessionSignals(
        session_name="x", pane_dead=True,
        pane_is_idle_placeholder=True,
    )) == SessionHealth.EXITED

    # auth broken → AUTH_BROKEN, not HEALTHY
    assert policy.classify(SessionSignals(
        session_name="x", auth_failure=True,
        pane_is_idle_placeholder=True,
    )) == SessionHealth.AUTH_BROKEN


def test_recovery_alert_auto_clear_works_for_placeholder_pane(
    monkeypatch, tmp_path: Path,
) -> None:
    """End-to-end #1010 contract: an alive Codex pane showing the
    placeholder hint, with an open ``recovery_limit`` alert past the
    debounce window, must auto-clear via the same sweep #1008 wired in.

    Pre-#1010 the issue was twofold: (a) the sweep wasn't actually
    invoked from the live heartbeat path, and (b) the heartbeat kept
    re-bumping ``stuck_session.updated_at`` on placeholder-showing
    panes because the classifier flagged them as unhealthy. This test
    pins (b) — the classifier no longer flags placeholder panes — by
    exercising the sweep directly with a tracked + window-alive
    session whose pane shows the Codex idle placeholder.
    """
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    launch = next(item for item in supervisor.plan_launches() if item.session.name == "worker")

    supervisor._msg_store.upsert_alert(
        launch.session.name, "recovery_limit", "error",
        "Automatic recovery paused after 5 rapid failures",
    )
    _backdate_alert(
        supervisor, launch.session.name, "recovery_limit",
        Supervisor._RECOVERY_ALERT_AUTO_CLEAR_DEBOUNCE_SECONDS + 30,
    )

    window_map = {launch.window_name: object()}
    name_by_window = {launch.window_name: launch.session.name}
    supervisor._sweep_recovered_recovery_alerts(
        window_map=window_map, name_by_window=name_by_window,
    )

    open_pairs = [
        (a.session_name, a.alert_type) for a in supervisor.open_alerts()
    ]
    assert (launch.session.name, "recovery_limit") not in open_pairs, (
        "alive-but-idle Codex pane showing the rotating placeholder hint "
        "must auto-clear recovery_limit after the debounce window"
    )


def test_run_heartbeat_invokes_recovery_alert_auto_clear_sweep(
    monkeypatch, tmp_path: Path,
) -> None:
    """#1010 — the auto-clear sweep is now wired into ``run_heartbeat``,
    not the dead ``_run_heartbeat_local`` method that #1008 originally
    targeted. Pin the wiring so a future refactor doesn't silently
    sever it again.
    """
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    invoked: list[dict] = []

    def _spy(*, window_map, name_by_window):
        invoked.append({"window_map": window_map, "name_by_window": name_by_window})

    monkeypatch.setattr(
        supervisor, "_sweep_recovered_recovery_alerts", _spy,
    )
    # Stub backend.run + scheduling so run_heartbeat doesn't try to
    # touch tmux / the recurring-job scheduler in the test process.
    monkeypatch.setattr(
        "pollypm.supervisor.get_heartbeat_backend",
        lambda name, root_dir=None: type(
            "_Backend", (), {"run": staticmethod(lambda api, snapshot_lines=200: [])},
        )(),
    )
    monkeypatch.setattr(supervisor, "ensure_heartbeat_schedule", lambda: None)
    monkeypatch.setattr(
        "pollypm.supervisor.sync_token_ledger_for_config",
        lambda config: [],
    )

    supervisor.run_heartbeat()

    assert invoked, (
        "_sweep_recovered_recovery_alerts must be invoked from run_heartbeat "
        "(post-#1010 wiring); pre-fix it lived only on dead _run_heartbeat_local"
    )


# ---------------------------------------------------------------------------
# #1019 — fd-exhaustion early-warning sweep
# ---------------------------------------------------------------------------


def test_check_fd_pressure_raises_warn_when_above_threshold(
    monkeypatch, tmp_path: Path,
) -> None:
    """#1019 — when open-fd usage crosses 80% of RLIMIT_NOFILE, the
    heartbeat sweep raises a ``warn`` alert so the operator can react
    before the next ``open()`` call returns ``Errno 24``."""
    config = _config(tmp_path)
    supervisor = Supervisor(config)

    # Pretend we have 900 open fds against a soft limit of 1000 (90%).
    monkeypatch.setattr("pollypm.supervisor._count_open_fds", lambda: 900)

    import resource as _resource

    monkeypatch.setattr(
        _resource,
        "getrlimit",
        lambda which: (1000, 1000) if which == _resource.RLIMIT_NOFILE else (0, 0),
    )

    raised: list[tuple] = []

    def _spy_upsert(session_name, alert_type, severity, message):
        raised.append((session_name, alert_type, severity, message))

    monkeypatch.setattr(supervisor._msg_store, "upsert_alert", _spy_upsert)
    monkeypatch.setattr(
        supervisor._msg_store, "clear_alert", lambda *a, **k: None,
    )

    supervisor._check_fd_pressure()

    assert raised, "expected fd_exhaustion_pending alert to be raised"
    session_name, alert_type, severity, message = raised[0]
    assert session_name == "heartbeat"
    assert alert_type == "fd_exhaustion_pending"
    assert severity == "warn"
    assert "1000" in message and "900" in message


def test_check_fd_pressure_clears_when_back_under_threshold(
    monkeypatch, tmp_path: Path,
) -> None:
    """The companion clear-path: when fd usage drops below the threshold,
    the previously-raised alert is cleared so the warning doesn't linger
    after a leak is fixed or a restart drains the count."""
    config = _config(tmp_path)
    supervisor = Supervisor(config)

    monkeypatch.setattr("pollypm.supervisor._count_open_fds", lambda: 100)

    import resource as _resource

    monkeypatch.setattr(
        _resource,
        "getrlimit",
        lambda which: (1000, 1000) if which == _resource.RLIMIT_NOFILE else (0, 0),
    )

    cleared: list[tuple] = []

    def _spy_clear(session_name, alert_type):
        cleared.append((session_name, alert_type))

    monkeypatch.setattr(
        supervisor._msg_store, "upsert_alert", lambda *a, **k: None,
    )
    monkeypatch.setattr(supervisor._msg_store, "clear_alert", _spy_clear)

    supervisor._check_fd_pressure()

    assert ("heartbeat", "fd_exhaustion_pending") in cleared


def test_check_fd_pressure_silent_when_count_unavailable(
    monkeypatch, tmp_path: Path,
) -> None:
    """On platforms that expose neither /proc/self/fd nor /dev/fd the
    sweep must no-op rather than raising spurious alerts."""
    config = _config(tmp_path)
    supervisor = Supervisor(config)

    monkeypatch.setattr("pollypm.supervisor._count_open_fds", lambda: None)

    raised: list = []
    monkeypatch.setattr(
        supervisor._msg_store,
        "upsert_alert",
        lambda *a, **k: raised.append(a),
    )
    monkeypatch.setattr(
        supervisor._msg_store,
        "clear_alert",
        lambda *a, **k: raised.append(a),
    )

    supervisor._check_fd_pressure()

    assert not raised
