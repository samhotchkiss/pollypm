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


# ---------------------------------------------------------------------------
# #249 — work-service-aware classifications (stuck_on_task / silent_worker)
# ---------------------------------------------------------------------------


class TestStuckOnTaskClassification:
    """stuck_on_task fires when a session sits on a claim > 30min with no
    events and no active turn. Mirrors acceptance criterion in #249."""

    def test_stuck_on_task_fires_at_threshold(self) -> None:
        policy = DefaultRecoveryPolicy()
        signals = SessionSignals(
            session_name="worker-foo",
            active_claim_task_id="foo/1",
            claim_age_seconds=1900,
            last_event_seconds_ago=1900,
        )
        assert policy.classify(signals) == SessionHealth.STUCK_ON_TASK

    def test_stuck_on_task_declines_when_turn_active(self) -> None:
        """Active turn = actively working, NOT stuck — classifier must not
        flag the session even though all the timers say so."""
        policy = DefaultRecoveryPolicy()
        signals = SessionSignals(
            session_name="worker-foo",
            active_claim_task_id="foo/1",
            claim_age_seconds=1900,
            last_event_seconds_ago=1900,
            turn_active=True,
        )
        # Turn active means has_transcript_delta-style liveness is true
        assert policy.classify(signals) != SessionHealth.STUCK_ON_TASK

    def test_stuck_on_task_declines_before_threshold(self) -> None:
        policy = DefaultRecoveryPolicy()
        signals = SessionSignals(
            session_name="worker-foo",
            active_claim_task_id="foo/1",
            claim_age_seconds=300,
            last_event_seconds_ago=300,
        )
        assert policy.classify(signals) != SessionHealth.STUCK_ON_TASK

    def test_stuck_on_task_declines_without_claim(self) -> None:
        policy = DefaultRecoveryPolicy()
        signals = SessionSignals(
            session_name="worker-foo",
            claim_age_seconds=1900,
            last_event_seconds_ago=1900,
        )
        assert policy.classify(signals) != SessionHealth.STUCK_ON_TASK

    def test_pane_dead_beats_stuck_on_task(self) -> None:
        """Mechanical failures must still win — a dead pane is EXITED, not
        stuck, so we send it through the relaunch path."""
        policy = DefaultRecoveryPolicy()
        signals = SessionSignals(
            session_name="worker-foo",
            active_claim_task_id="foo/1",
            claim_age_seconds=1900,
            last_event_seconds_ago=1900,
            pane_dead=True,
        )
        assert policy.classify(signals) == SessionHealth.EXITED

    def test_intervention_is_resume_ping(self) -> None:
        policy = DefaultRecoveryPolicy()
        signals = SessionSignals(
            session_name="worker-foo",
            active_claim_task_id="foo/1",
            claim_age_seconds=1900,
            last_event_seconds_ago=1900,
        )
        result = policy.select_intervention(
            SessionHealth.STUCK_ON_TASK, signals, [],
        )
        assert result is not None
        assert result.action == "resume_ping"
        assert result.details["task_id"] == "foo/1"


class TestSilentWorkerClassification:
    """silent_worker fires when a worker role is alive, has no claim, and
    hasn't recorded an event in 30min (missed a queue signal)."""

    def test_silent_worker_fires_for_worker_role(self) -> None:
        policy = DefaultRecoveryPolicy()
        signals = SessionSignals(
            session_name="worker-demo",
            session_role="worker",
            last_event_seconds_ago=1900,
        )
        assert policy.classify(signals) == SessionHealth.SILENT_WORKER

    def test_silent_worker_does_not_fire_for_operator(self) -> None:
        policy = DefaultRecoveryPolicy()
        signals = SessionSignals(
            session_name="operator",
            session_role="operator-pm",
            last_event_seconds_ago=1900,
        )
        assert policy.classify(signals) != SessionHealth.SILENT_WORKER

    def test_silent_worker_declines_when_claim_present(self) -> None:
        policy = DefaultRecoveryPolicy()
        signals = SessionSignals(
            session_name="worker-demo",
            session_role="worker",
            active_claim_task_id="demo/5",
            last_event_seconds_ago=1900,
        )
        # With a claim the classifier should prefer stuck_on_task path
        # (since claim_age is None here → does not qualify), so it falls
        # through to HEALTHY — not silent_worker.
        assert policy.classify(signals) != SessionHealth.SILENT_WORKER

    def test_silent_worker_declines_when_turn_active(self) -> None:
        policy = DefaultRecoveryPolicy()
        signals = SessionSignals(
            session_name="worker-demo",
            session_role="worker",
            last_event_seconds_ago=1900,
            turn_active=True,
        )
        assert policy.classify(signals) != SessionHealth.SILENT_WORKER

    def test_intervention_is_prompt_pm_task_next(self) -> None:
        policy = DefaultRecoveryPolicy()
        signals = SessionSignals(
            session_name="worker-demo",
            session_role="worker",
            last_event_seconds_ago=1900,
        )
        result = policy.select_intervention(
            SessionHealth.SILENT_WORKER, signals, [],
        )
        assert result is not None
        assert result.action == "prompt_pm_task_next"
