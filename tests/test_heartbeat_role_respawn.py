"""Regression tests for #1506 role-respawn-on-crash.

When a worker/architect/reviewer pane returns to the shell prompt
(Claude/codex process exited but tmux pane still alive) AND the role
has active work, the heartbeat should:

1. Set the session status to ``recovering``
2. Call ``recover_session(failure_type="role_crashed", ...)`` so the
   supervisor relaunches the pane within this tick
3. Append ``role_crashed`` to the alerts list

If the session has already had ``_CRASH_LOOP_ATTEMPT_THRESHOLD`` or
more cumulative recoveries, the heartbeat surfaces a ``crash_loop``
alert to the operator instead of relaunching again.

Idle role panes (no pending work) and stalled-but-running panes
(Claude PID alive, just silent) are explicitly NOT auto-respawned —
those have their own paths (the existing nudge ladder via #1495 /
#1505).
"""

from __future__ import annotations

from types import SimpleNamespace

from pollypm.heartbeats.base import HeartbeatSessionContext
from pollypm.heartbeats.local import LocalHeartbeatBackend


# ---------------------------------------------------------------------------
# Fake API — minimal stub that records the calls we assert on
# ---------------------------------------------------------------------------


class _RoleCrashFakeAPI:
    """Records recover_session calls, status sets, and alert emits.

    No-ops everything else _process_session needs so the test path
    runs cleanly. Generous enough to exercise the full block without
    depending on real Supervisor / store wiring.
    """

    def __init__(self, *, recovery_attempts: int = 0) -> None:
        self.recover_calls: list[dict] = []
        self.statuses: dict[str, tuple[str, str]] = {}
        self.alerts_raised: list[dict] = []
        self.cleared_alerts: list[tuple[str, str]] = []
        self.cursor_updates: list[dict] = []
        self.checkpoints: list[tuple[str, list[str]]] = []
        runtime = SimpleNamespace(
            recovery_attempts=recovery_attempts,
            status="healthy",
        )
        self.supervisor = SimpleNamespace(
            store=SimpleNamespace(
                get_session_runtime=lambda _name: runtime,
            ),
            config=SimpleNamespace(sessions={}, projects={}),
            msg_store=None,
        )

    # _process_session protocol ---------------------------------------
    def list_sessions(self):
        return []

    def list_unmanaged_windows(self):
        return []

    def update_cursor(self, *_args, **kwargs) -> None:
        self.cursor_updates.append(dict(kwargs))

    def record_observation(self, _context) -> None:
        pass

    def record_checkpoint(self, context, *, alerts) -> None:
        self.checkpoints.append((context.session_name, list(alerts)))

    def record_event(self, *_args, **_kwargs) -> None:
        pass

    def raise_alert(
        self,
        session_name: str,
        alert_type: str,
        severity: str,
        message: str,
    ) -> None:
        self.alerts_raised.append({
            "session_name": session_name,
            "alert_type": alert_type,
            "severity": severity,
            "message": message,
        })

    def clear_alert(self, session_name, alert_type) -> None:
        self.cleared_alerts.append((session_name, alert_type))

    def open_alerts(self):
        return []

    def set_session_status(self, session_name, status, *, reason="") -> None:
        self.statuses[session_name] = (status, reason)

    def mark_account_auth_broken(self, *_args, **_kwargs) -> None:
        pass

    def recent_snapshot_hashes(self, *_args, **_kwargs):
        return []

    def recover_session(self, session_name, *, failure_type, message) -> None:
        self.recover_calls.append({
            "session_name": session_name,
            "failure_type": failure_type,
            "message": message,
        })

    def send_session_message(self, *_args, **_kwargs) -> None:
        pass

    def queue_polly_followup(self, *_args, **_kwargs) -> None:
        pass


def _crashed_pane_context(
    *,
    role: str = "architect",
    session_name: str = "architect_x",
    pane_command: str = "zsh",
) -> HeartbeatSessionContext:
    """Pane that has returned to a shell prompt — Claude/codex
    process exited but tmux pane itself is still alive."""
    return HeartbeatSessionContext(
        session_name=session_name,
        role=role,
        project_key="proj",
        provider="claude",
        account_name="acct",
        cwd="/workspace",
        tmux_session="ses",
        window_name=session_name,
        source_path=f"/tmp/{session_name}.log",
        source_bytes=64,
        transcript_delta="",
        pane_text="❯ ",
        snapshot_path=f"/tmp/{session_name}.txt",
        snapshot_hash="hash-1",
        pane_id="%1",
        pane_command=pane_command,
        pane_dead=False,
        window_present=True,
        previous_log_bytes=64,
        previous_snapshot_hash="hash-0",
        cursor=None,
    )


def _alive_role_context(
    *,
    role: str = "architect",
    session_name: str = "architect_x",
) -> HeartbeatSessionContext:
    """Same role, pane still running claude (NOT a shell). Used to
    confirm the new block does NOT fire for live roles."""
    return HeartbeatSessionContext(
        session_name=session_name,
        role=role,
        project_key="proj",
        provider="claude",
        account_name="acct",
        cwd="/workspace",
        tmux_session="ses",
        window_name=session_name,
        source_path=f"/tmp/{session_name}.log",
        source_bytes=64,
        transcript_delta="",
        pane_text="Working on the plan…",
        snapshot_path=f"/tmp/{session_name}.txt",
        snapshot_hash="hash-1",
        pane_id="%1",
        pane_command="claude",
        pane_dead=False,
        window_present=True,
        previous_log_bytes=64,
        previous_snapshot_hash="hash-0",
        cursor=None,
    )


def _backend_with_pending_work(*, has_work: bool) -> LocalHeartbeatBackend:
    backend = LocalHeartbeatBackend()
    backend._has_pending_work = lambda _api, _ctx: has_work  # type: ignore[method-assign]
    return backend


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_crashed_architect_with_active_work_auto_respawns() -> None:
    """Architect pane returned to shell + project has pending work →
    recover_session called with failure_type=role_crashed in the
    same tick."""
    api = _RoleCrashFakeAPI(recovery_attempts=0)
    backend = _backend_with_pending_work(has_work=True)

    backend._process_session(api, _crashed_pane_context(role="architect"))

    assert len(api.recover_calls) == 1
    call = api.recover_calls[0]
    assert call["session_name"] == "architect_x"
    assert call["failure_type"] == "role_crashed"
    assert "shell" in call["message"]


def test_crashed_reviewer_with_active_work_auto_respawns() -> None:
    """Same path for reviewer role."""
    api = _RoleCrashFakeAPI(recovery_attempts=0)
    backend = _backend_with_pending_work(has_work=True)

    backend._process_session(
        api, _crashed_pane_context(role="reviewer", session_name="reviewer_x")
    )

    assert len(api.recover_calls) == 1
    assert api.recover_calls[0]["failure_type"] == "role_crashed"


def test_crashed_worker_with_active_work_auto_respawns() -> None:
    """Workers crash too; same recovery path."""
    api = _RoleCrashFakeAPI(recovery_attempts=0)
    backend = _backend_with_pending_work(has_work=True)

    backend._process_session(
        api, _crashed_pane_context(role="worker", session_name="worker_x")
    )

    assert any(
        c["session_name"] == "worker_x" and c["failure_type"] == "role_crashed"
        for c in api.recover_calls
    )


def test_crashed_architect_without_active_work_does_not_respawn() -> None:
    """Idle architect pane (no pending project work) returned to
    shell — cleanly fine, no auto-respawn. The shell_returned alert
    still fires (separate path) but no recover_session call for
    role_crashed."""
    api = _RoleCrashFakeAPI(recovery_attempts=0)
    backend = _backend_with_pending_work(has_work=False)

    backend._process_session(api, _crashed_pane_context(role="architect"))

    crashed_calls = [
        c for c in api.recover_calls if c["failure_type"] == "role_crashed"
    ]
    assert crashed_calls == []


def test_crash_loop_threshold_escalates_instead_of_respawning() -> None:
    """After _CRASH_LOOP_ATTEMPT_THRESHOLD recoveries, a fresh crash
    surfaces ``crash_loop`` alert and does NOT call recover_session
    again."""
    api = _RoleCrashFakeAPI(
        recovery_attempts=LocalHeartbeatBackend._CRASH_LOOP_ATTEMPT_THRESHOLD,
    )
    backend = _backend_with_pending_work(has_work=True)

    backend._process_session(api, _crashed_pane_context(role="architect"))

    crashed_calls = [
        c for c in api.recover_calls if c["failure_type"] == "role_crashed"
    ]
    assert crashed_calls == []
    assert any(
        a.get("alert_type") == "crash_loop"
        for a in api.alerts_raised
    )


def test_alive_role_pane_does_not_trigger_role_crashed_recovery() -> None:
    """Pane still running claude (pane_command='claude') — the
    role_crashed block must NOT fire even if has_pending_work=True.
    That path is for stalled-but-running and uses the nudge ladder
    via #1495 / #1505."""
    api = _RoleCrashFakeAPI(recovery_attempts=0)
    backend = _backend_with_pending_work(has_work=True)

    backend._process_session(api, _alive_role_context(role="architect"))

    crashed_calls = [
        c for c in api.recover_calls if c["failure_type"] == "role_crashed"
    ]
    assert crashed_calls == []


def test_role_crashed_appends_to_alerts_checkpoint() -> None:
    """The new code appends ``role_crashed`` to the alerts list, so
    the per-tick checkpoint records it (used by the activity feed)."""
    api = _RoleCrashFakeAPI(recovery_attempts=0)
    backend = _backend_with_pending_work(has_work=True)

    backend._process_session(api, _crashed_pane_context(role="architect"))

    # checkpoints recorded per session: (session_name, alerts_list)
    architect_checkpoints = [
        cp for cp in api.checkpoints if cp[0] == "architect_x"
    ]
    assert architect_checkpoints, "expected a checkpoint for architect_x"
    assert "role_crashed" in architect_checkpoints[-1][1]


def test_crash_loop_marker_clears_when_pane_recovers() -> None:
    """When the pane is no longer at a shell prompt (next tick after
    successful respawn), clear_alert is called for ``crash_loop`` —
    so a stale crash_loop alert from a previous run doesn't linger."""
    api = _RoleCrashFakeAPI(recovery_attempts=0)
    backend = _backend_with_pending_work(has_work=True)

    backend._process_session(api, _alive_role_context(role="architect"))

    cleared_types = [t for (_s, t) in api.cleared_alerts]
    assert "crash_loop" in cleared_types


def test_crash_recovery_roles_constants_match_spec() -> None:
    """Sentinel: the role list is locked to worker/architect/reviewer.
    Operator-pm and heartbeat-supervisor are deliberately excluded."""
    assert LocalHeartbeatBackend._CRASH_RECOVERY_ROLES == frozenset(
        {"worker", "architect", "reviewer"}
    )
    assert LocalHeartbeatBackend._CRASH_LOOP_ATTEMPT_THRESHOLD == 2
