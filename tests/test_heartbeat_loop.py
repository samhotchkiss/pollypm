"""Unit tests for the heartbeat supervision loop."""

from pathlib import Path

from pollypm.capacity import CapacityState
from pollypm.heartbeat_loop import (
    HeartbeatCycleResult,
    InterventionAction,
    INTERVENTION_LADDER,
    SessionHealth,
    SessionSignals,
    classify_session_health,
    select_intervention,
)


# ---------------------------------------------------------------------------
# Health classification
# ---------------------------------------------------------------------------


class TestClassifySessionHealth:
    def test_exited_when_window_missing(self) -> None:
        signals = SessionSignals(session_name="w", window_present=False)
        assert classify_session_health(signals) == SessionHealth.EXITED

    def test_exited_when_pane_dead(self) -> None:
        signals = SessionSignals(session_name="w", pane_dead=True)
        assert classify_session_health(signals) == SessionHealth.EXITED

    def test_auth_broken(self) -> None:
        signals = SessionSignals(session_name="w", auth_failure=True)
        assert classify_session_health(signals) == SessionHealth.AUTH_BROKEN

    def test_blocked_no_capacity(self) -> None:
        signals = SessionSignals(
            session_name="w",
            capacity_state=CapacityState.EXHAUSTED,
        )
        assert classify_session_health(signals) == SessionHealth.BLOCKED_NO_CAPACITY

    def test_looping_after_3_repeats(self) -> None:
        signals = SessionSignals(session_name="w", snapshot_repeated=3)
        assert classify_session_health(signals) == SessionHealth.LOOPING

    def test_not_looping_at_2(self) -> None:
        signals = SessionSignals(session_name="w", snapshot_repeated=2)
        assert classify_session_health(signals) != SessionHealth.LOOPING

    def test_stuck_after_idle_cycles(self) -> None:
        signals = SessionSignals(
            session_name="w",
            output_stale=True,
            idle_cycles=3,
        )
        assert classify_session_health(signals) == SessionHealth.STUCK

    def test_idle(self) -> None:
        signals = SessionSignals(
            session_name="w",
            output_stale=True,
            idle_cycles=1,
        )
        assert classify_session_health(signals) == SessionHealth.IDLE

    def test_active(self) -> None:
        signals = SessionSignals(
            session_name="w",
            has_transcript_delta=True,
        )
        assert classify_session_health(signals) == SessionHealth.ACTIVE

    def test_waiting_on_user_when_blocked_verdict(self) -> None:
        signals = SessionSignals(session_name="w", last_verdict="blocked")
        assert classify_session_health(signals) == SessionHealth.WAITING_ON_USER

    def test_waiting_on_user_takes_precedence_over_stuck(self) -> None:
        signals = SessionSignals(
            session_name="w",
            output_stale=True,
            idle_cycles=5,
            last_verdict="blocked",
        )
        assert classify_session_health(signals) == SessionHealth.WAITING_ON_USER

    def test_healthy_default(self) -> None:
        signals = SessionSignals(session_name="w")
        assert classify_session_health(signals) == SessionHealth.HEALTHY


# ---------------------------------------------------------------------------
# Intervention selection
# ---------------------------------------------------------------------------


class TestSelectIntervention:
    def test_no_intervention_when_healthy(self) -> None:
        signals = SessionSignals(session_name="w")
        result = select_intervention(SessionHealth.HEALTHY, signals)
        assert result is None

    def test_no_intervention_when_active(self) -> None:
        signals = SessionSignals(session_name="w")
        result = select_intervention(SessionHealth.ACTIVE, signals)
        assert result is None

    def test_nudge_on_first_idle(self) -> None:
        signals = SessionSignals(session_name="w")
        result = select_intervention(
            SessionHealth.IDLE, signals,
            previous_interventions=0,
        )
        assert result is not None
        assert result.action == "nudge"

    def test_reset_on_second_idle(self) -> None:
        signals = SessionSignals(session_name="w")
        result = select_intervention(
            SessionHealth.IDLE, signals,
            previous_interventions=1,
        )
        assert result is not None
        assert result.action == "reset"

    def test_escalate_on_persistent_idle(self) -> None:
        signals = SessionSignals(session_name="w")
        result = select_intervention(
            SessionHealth.IDLE, signals,
            previous_interventions=3,
        )
        assert result is not None
        assert result.action == "escalate"

    def test_reset_on_stuck(self) -> None:
        signals = SessionSignals(session_name="w")
        result = select_intervention(SessionHealth.STUCK, signals)
        assert result is not None
        assert result.action == "reset"

    def test_relaunch_on_stuck_after_resets(self) -> None:
        signals = SessionSignals(session_name="w")
        result = select_intervention(
            SessionHealth.STUCK, signals,
            previous_interventions=2,
        )
        assert result is not None
        assert result.action == "relaunch"

    def test_reset_on_looping(self) -> None:
        signals = SessionSignals(session_name="w")
        result = select_intervention(SessionHealth.LOOPING, signals)
        assert result is not None
        assert result.action == "reset"

    def test_relaunch_on_exited(self) -> None:
        signals = SessionSignals(session_name="w")
        result = select_intervention(SessionHealth.EXITED, signals)
        assert result is not None
        assert result.action == "relaunch"

    def test_failover_on_auth_broken(self) -> None:
        signals = SessionSignals(session_name="w")
        result = select_intervention(SessionHealth.AUTH_BROKEN, signals)
        assert result is not None
        assert result.action == "failover"

    def test_failover_on_blocked_capacity(self) -> None:
        signals = SessionSignals(session_name="w")
        result = select_intervention(SessionHealth.BLOCKED_NO_CAPACITY, signals)
        assert result is not None
        assert result.action == "failover"

    def test_nudge_intervention_waiting_on_user(self) -> None:
        # Workers waiting on user get a nudge — triage decides whether to push forward.
        signals = SessionSignals(session_name="w")
        result = select_intervention(SessionHealth.WAITING_ON_USER, signals)
        assert result is not None
        assert result.action == "nudge"

    def test_no_intervention_for_idle_operator(self) -> None:
        signals = SessionSignals(session_name="operator")
        result = select_intervention(
            SessionHealth.IDLE, signals,
            previous_interventions=3,
        )
        assert result is None

    def test_no_intervention_for_operator_waiting_on_user(self) -> None:
        signals = SessionSignals(session_name="operator")
        result = select_intervention(SessionHealth.WAITING_ON_USER, signals)
        assert result is None

    def test_escalate_on_error(self) -> None:
        signals = SessionSignals(session_name="w")
        result = select_intervention(SessionHealth.ERROR, signals)
        assert result is not None
        assert result.action == "escalate"


# ---------------------------------------------------------------------------
# Intervention ladder
# ---------------------------------------------------------------------------


class TestInterventionLadder:
    def test_ladder_order(self) -> None:
        assert INTERVENTION_LADDER == ("nudge", "reset", "relaunch", "failover", "escalate")

    def test_all_actions_valid(self) -> None:
        for action in INTERVENTION_LADDER:
            assert isinstance(action, str)
            assert len(action) > 0


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


class TestSessionSignals:
    def test_defaults(self) -> None:
        signals = SessionSignals(session_name="test")
        assert signals.window_present is True
        assert signals.pane_dead is False
        assert signals.idle_cycles == 0
        assert signals.capacity_state == CapacityState.UNKNOWN


class TestHeartbeatCycleResult:
    def test_defaults(self) -> None:
        result = HeartbeatCycleResult(timestamp="2026-04-10T00:00:00Z")
        assert result.sessions_checked == 0
        assert result.interventions == []
        assert result.checkpoints_recorded == 0
