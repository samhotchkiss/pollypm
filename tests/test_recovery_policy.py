"""Unit tests for the pluggable RecoveryPolicy.

The default policy is the sealed successor to the historical
``heartbeat_loop`` ``classify_session_health`` / ``select_intervention``
helpers (removed in issue #166). These tests pin the classification
ladder contract.
"""

from pollypm.capacity import CapacityState
from pollypm.recovery.base import (
    INTERVENTION_LADDER,
    InterventionAction,
    InterventionHistoryEntry,
    SessionHealth,
    SessionSignals,
)
from pollypm.recovery.default import DefaultRecoveryPolicy


# ---------------------------------------------------------------------------
# Reference signal set — classify() must match legacy exactly
# ---------------------------------------------------------------------------


REFERENCE_CLASSIFICATIONS: list[tuple[SessionSignals, SessionHealth]] = [
    (SessionSignals(session_name="w"), SessionHealth.HEALTHY),
    (SessionSignals(session_name="w", has_transcript_delta=True), SessionHealth.ACTIVE),
    (SessionSignals(session_name="w", window_present=False), SessionHealth.EXITED),
    (SessionSignals(session_name="w", pane_dead=True), SessionHealth.EXITED),
    (SessionSignals(session_name="w", auth_failure=True), SessionHealth.AUTH_BROKEN),
    (
        SessionSignals(session_name="w", capacity_state=CapacityState.EXHAUSTED),
        SessionHealth.BLOCKED_NO_CAPACITY,
    ),
    (
        SessionSignals(session_name="w", capacity_state=CapacityState.THROTTLED),
        SessionHealth.BLOCKED_NO_CAPACITY,
    ),
    (
        SessionSignals(session_name="w", last_verdict="blocked"),
        SessionHealth.WAITING_ON_USER,
    ),
    (SessionSignals(session_name="w", snapshot_repeated=3), SessionHealth.LOOPING),
    (
        SessionSignals(session_name="w", output_stale=True, idle_cycles=3),
        SessionHealth.STUCK,
    ),
    (
        SessionSignals(session_name="w", output_stale=True, idle_cycles=1),
        SessionHealth.IDLE,
    ),
    (
        SessionSignals(
            session_name="w",
            output_stale=True,
            idle_cycles=5,
            last_verdict="blocked",
        ),
        SessionHealth.WAITING_ON_USER,
    ),
]


class TestDefaultRecoveryPolicyClassify:
    def test_reference_classifications(self) -> None:
        policy = DefaultRecoveryPolicy()
        for signals, expected in REFERENCE_CLASSIFICATIONS:
            assert policy.classify(signals) == expected, (
                f"classify disagreement for {signals!r}: expected {expected}"
            )

    def test_healthy_default(self) -> None:
        policy = DefaultRecoveryPolicy()
        assert policy.classify(SessionSignals(session_name="w")) == SessionHealth.HEALTHY

    def test_window_missing_beats_pane_dead(self) -> None:
        policy = DefaultRecoveryPolicy()
        signals = SessionSignals(session_name="w", window_present=False, pane_dead=True)
        assert policy.classify(signals) == SessionHealth.EXITED


# ---------------------------------------------------------------------------
# Intervention ladder — select_intervention() must match legacy exactly
# ---------------------------------------------------------------------------


def _history(n: int) -> list[InterventionHistoryEntry]:
    return [InterventionHistoryEntry(action="") for _ in range(n)]


class TestDefaultRecoveryPolicySelect:
    def test_no_intervention_when_healthy(self) -> None:
        policy = DefaultRecoveryPolicy()
        signals = SessionSignals(session_name="w")
        assert policy.select_intervention(SessionHealth.HEALTHY, signals, []) is None

    def test_no_intervention_when_active(self) -> None:
        policy = DefaultRecoveryPolicy()
        signals = SessionSignals(session_name="w")
        assert policy.select_intervention(SessionHealth.ACTIVE, signals, []) is None

    def test_nudge_on_first_idle(self) -> None:
        policy = DefaultRecoveryPolicy()
        signals = SessionSignals(session_name="w")
        result = policy.select_intervention(SessionHealth.IDLE, signals, _history(0))
        assert result is not None and result.action == "nudge"

    def test_reset_on_second_idle(self) -> None:
        policy = DefaultRecoveryPolicy()
        signals = SessionSignals(session_name="w")
        result = policy.select_intervention(SessionHealth.IDLE, signals, _history(1))
        assert result is not None and result.action == "reset"

    def test_escalate_on_persistent_idle(self) -> None:
        policy = DefaultRecoveryPolicy()
        signals = SessionSignals(session_name="w")
        result = policy.select_intervention(SessionHealth.IDLE, signals, _history(3))
        assert result is not None and result.action == "escalate"

    def test_reset_on_stuck(self) -> None:
        policy = DefaultRecoveryPolicy()
        signals = SessionSignals(session_name="w")
        result = policy.select_intervention(SessionHealth.STUCK, signals, _history(0))
        assert result is not None and result.action == "reset"

    def test_relaunch_on_stuck_after_resets(self) -> None:
        policy = DefaultRecoveryPolicy()
        signals = SessionSignals(session_name="w")
        result = policy.select_intervention(SessionHealth.STUCK, signals, _history(2))
        assert result is not None and result.action == "relaunch"

    def test_reset_on_looping(self) -> None:
        policy = DefaultRecoveryPolicy()
        signals = SessionSignals(session_name="w")
        result = policy.select_intervention(SessionHealth.LOOPING, signals, _history(0))
        assert result is not None and result.action == "reset"

    def test_relaunch_on_looping_after_resets(self) -> None:
        policy = DefaultRecoveryPolicy()
        signals = SessionSignals(session_name="w")
        result = policy.select_intervention(SessionHealth.LOOPING, signals, _history(2))
        assert result is not None and result.action == "relaunch"

    def test_relaunch_on_exited(self) -> None:
        policy = DefaultRecoveryPolicy()
        signals = SessionSignals(session_name="w")
        result = policy.select_intervention(SessionHealth.EXITED, signals, [])
        assert result is not None and result.action == "relaunch"

    def test_failover_on_auth_broken(self) -> None:
        policy = DefaultRecoveryPolicy()
        signals = SessionSignals(session_name="w")
        result = policy.select_intervention(SessionHealth.AUTH_BROKEN, signals, [])
        assert result is not None and result.action == "failover"

    def test_failover_on_blocked_capacity(self) -> None:
        policy = DefaultRecoveryPolicy()
        signals = SessionSignals(session_name="w")
        result = policy.select_intervention(
            SessionHealth.BLOCKED_NO_CAPACITY, signals, [],
        )
        assert result is not None and result.action == "failover"

    def test_nudge_on_waiting_on_user_for_worker(self) -> None:
        policy = DefaultRecoveryPolicy()
        signals = SessionSignals(session_name="worker_a")
        result = policy.select_intervention(SessionHealth.WAITING_ON_USER, signals, [])
        assert result is not None and result.action == "nudge"

    def test_no_intervention_for_operator_waiting_on_user(self) -> None:
        policy = DefaultRecoveryPolicy()
        signals = SessionSignals(session_name="operator")
        result = policy.select_intervention(SessionHealth.WAITING_ON_USER, signals, [])
        assert result is None

    def test_no_intervention_for_idle_operator(self) -> None:
        policy = DefaultRecoveryPolicy()
        signals = SessionSignals(session_name="operator")
        result = policy.select_intervention(SessionHealth.IDLE, signals, _history(3))
        assert result is None

    def test_escalate_on_error(self) -> None:
        policy = DefaultRecoveryPolicy()
        signals = SessionSignals(session_name="w")
        result = policy.select_intervention(SessionHealth.ERROR, signals, [])
        assert result is not None and result.action == "escalate"


# ---------------------------------------------------------------------------
# Plugin-host registration
# ---------------------------------------------------------------------------


class TestRecoveryPolicyPlugin:
    def test_default_policy_resolves_through_plugin_host(self, tmp_path) -> None:
        from pollypm.plugin_host import ExtensionHost

        host = ExtensionHost(tmp_path)
        policy = host.get_recovery_policy("default")
        assert policy is not None
        assert isinstance(policy, DefaultRecoveryPolicy)
        assert policy.name == "default"

    def test_plugin_api_supports_recovery_policies_field(self) -> None:
        from pollypm.plugin_api.v1 import PollyPMPlugin

        plugin = PollyPMPlugin(name="x")
        assert plugin.recovery_policies == {}


# ---------------------------------------------------------------------------
# Ladder is canonical
# ---------------------------------------------------------------------------


class TestLadderConstant:
    def test_ladder_order(self) -> None:
        assert INTERVENTION_LADDER == ("nudge", "reset", "relaunch", "failover", "escalate")


# Smoke: InterventionAction dataclass still constructible the old way.
def test_intervention_action_defaults() -> None:
    action = InterventionAction(session_name="w", action="nudge", reason="why")
    assert action.details == {}
