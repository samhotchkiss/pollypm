"""Integration tests for the heartbeat supervision loop."""

from pathlib import Path

from pollypm.capacity import CapacityState
from pollypm.heartbeat_loop import (
    HeartbeatCycleResult,
    SessionHealth,
    SessionSignals,
    assess_project_state,
    classify_session_health,
    run_heartbeat_cycle,
    select_intervention,
)
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
from pollypm.storage.state import StateStore


def _config(tmp_path: Path) -> PollyPMConfig:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    return PollyPMConfig(
        project=ProjectSettings(
            root_dir=project_root,
            base_dir=project_root / ".pollypm-state",
            logs_dir=project_root / ".pollypm-state/logs",
            snapshots_dir=project_root / ".pollypm-state/snapshots",
            state_db=project_root / ".pollypm-state/state.db",
        ),
        pollypm=PollyPMSettings(
            controller_account="claude_main",
            failover_enabled=True,
            failover_accounts=["claude_backup"],
        ),
        accounts={
            "claude_main": AccountConfig(
                name="claude_main",
                provider=ProviderKind.CLAUDE,
                home=project_root / ".pollypm-state" / "homes" / "claude_main",
            ),
            "claude_backup": AccountConfig(
                name="claude_backup",
                provider=ProviderKind.CLAUDE,
                home=project_root / ".pollypm-state" / "homes" / "claude_backup",
            ),
        },
        sessions={
            "heartbeat": SessionConfig(
                name="heartbeat",
                role="heartbeat-supervisor",
                provider=ProviderKind.CLAUDE,
                account="claude_main",
                cwd=project_root,
            ),
            "operator": SessionConfig(
                name="operator",
                role="operator-pm",
                provider=ProviderKind.CLAUDE,
                account="claude_main",
                cwd=project_root,
            ),
            "worker_a": SessionConfig(
                name="worker_a",
                role="worker",
                provider=ProviderKind.CLAUDE,
                account="claude_main",
                cwd=project_root,
            ),
        },
        projects={},
    )


def test_heartbeat_cycle_all_healthy(tmp_path: Path) -> None:
    """All sessions healthy → no interventions."""
    config = _config(tmp_path)
    store = StateStore(config.project.state_db)

    signals = [
        SessionSignals(session_name="heartbeat", has_transcript_delta=True),
        SessionSignals(session_name="operator", has_transcript_delta=True),
        SessionSignals(session_name="worker_a", has_transcript_delta=True),
    ]

    result = run_heartbeat_cycle(config, store, session_signals=signals)

    assert result.sessions_checked == 3
    assert len(result.interventions) == 0
    assert all(h == SessionHealth.ACTIVE for h in result.classifications.values())
    assert result.checkpoints_recorded == 3


def test_heartbeat_cycle_idle_session_nudged(tmp_path: Path) -> None:
    """An idle session should get a nudge intervention."""
    config = _config(tmp_path)
    store = StateStore(config.project.state_db)

    signals = [
        SessionSignals(session_name="heartbeat", has_transcript_delta=True),
        SessionSignals(session_name="operator", has_transcript_delta=True),
        SessionSignals(session_name="worker_a", output_stale=True, idle_cycles=1),
    ]

    result = run_heartbeat_cycle(config, store, session_signals=signals)

    assert result.classifications["worker_a"] == SessionHealth.IDLE
    assert any(i.session_name == "worker_a" and i.action == "nudge" for i in result.interventions)


def test_heartbeat_cycle_idle_operator_is_not_escalated(tmp_path: Path) -> None:
    """An idle operator at the prompt should not generate interventions."""
    config = _config(tmp_path)
    store = StateStore(config.project.state_db)
    store.upsert_session_runtime(
        session_name="operator",
        status="idle",
        recovery_attempts=3,
    )

    signals = [
        SessionSignals(session_name="operator", output_stale=True, idle_cycles=1),
    ]

    result = run_heartbeat_cycle(config, store, session_signals=signals)

    assert result.classifications["operator"] == SessionHealth.IDLE
    assert result.interventions == []


def test_heartbeat_cycle_exited_session_relaunched(tmp_path: Path) -> None:
    """An exited session should trigger relaunch."""
    config = _config(tmp_path)
    store = StateStore(config.project.state_db)

    signals = [
        SessionSignals(session_name="worker_a", pane_dead=True),
    ]

    result = run_heartbeat_cycle(config, store, session_signals=signals)

    assert result.classifications["worker_a"] == SessionHealth.EXITED
    assert any(i.session_name == "worker_a" and i.action == "relaunch" for i in result.interventions)


def test_heartbeat_cycle_auth_broken_triggers_failover(tmp_path: Path) -> None:
    """Auth failure should trigger failover."""
    config = _config(tmp_path)
    store = StateStore(config.project.state_db)

    signals = [
        SessionSignals(session_name="worker_a", auth_failure=True),
    ]

    result = run_heartbeat_cycle(config, store, session_signals=signals)

    assert result.classifications["worker_a"] == SessionHealth.AUTH_BROKEN
    assert any(i.session_name == "worker_a" and i.action == "failover" for i in result.interventions)


def test_heartbeat_cycle_human_lease_blocks_failover(tmp_path: Path) -> None:
    """Failover should be blocked by human lease, escalating instead."""
    config = _config(tmp_path)
    store = StateStore(config.project.state_db)

    # Set human lease
    store.set_lease("worker_a", "human", "user is typing")

    signals = [
        SessionSignals(session_name="worker_a", auth_failure=True),
    ]

    result = run_heartbeat_cycle(config, store, session_signals=signals)

    # Should escalate instead of failover due to lease
    assert any(
        i.session_name == "worker_a" and i.action == "escalate"
        for i in result.interventions
    )


def test_heartbeat_cycle_stuck_escalation(tmp_path: Path) -> None:
    """Stuck sessions should escalate through reset → relaunch."""
    config = _config(tmp_path)
    store = StateStore(config.project.state_db)

    # First cycle: stuck → reset
    signals = [
        SessionSignals(session_name="worker_a", output_stale=True, idle_cycles=3),
    ]
    result = run_heartbeat_cycle(config, store, session_signals=signals)
    assert result.classifications["worker_a"] == SessionHealth.STUCK
    assert any(i.action == "reset" for i in result.interventions)

    # Simulate previous recovery attempts
    store.upsert_session_runtime(
        session_name="worker_a",
        status="stuck",
        recovery_attempts=2,
    )

    # Second cycle: still stuck → relaunch
    result = run_heartbeat_cycle(config, store, session_signals=signals)
    assert any(i.action == "relaunch" for i in result.interventions)


def test_assess_project_state(tmp_path: Path) -> None:
    """Quick project state assessment should include sessions, alerts, capacity."""
    config = _config(tmp_path)
    store = StateStore(config.project.state_db)

    store.upsert_session_runtime(session_name="worker_a", status="healthy")
    store.upsert_alert("worker_a", "idle_output", "warn", "No output")

    state = assess_project_state(config, store, "pollypm")

    assert "worker_a" in state["sessions"]
    assert state["sessions"]["worker_a"]["status"] == "healthy"
    assert len(state["open_alerts"]) >= 1
    assert "claude_main" in state["capacity"]


def test_looping_detection(tmp_path: Path) -> None:
    """Three identical snapshots should classify as looping."""
    config = _config(tmp_path)
    store = StateStore(config.project.state_db)

    signals = [
        SessionSignals(session_name="worker_a", snapshot_repeated=3),
    ]

    result = run_heartbeat_cycle(config, store, session_signals=signals)
    assert result.classifications["worker_a"] == SessionHealth.LOOPING
    assert any(i.action == "reset" for i in result.interventions)
