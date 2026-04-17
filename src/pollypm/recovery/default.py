"""Built-in default recovery policy.

This is the sealed classification + escalation ladder; its historical
ancestor lived in ``pollypm.heartbeat_loop.classify_session_health`` /
``select_intervention`` before issue #166 removed the legacy dispatch.
See ``tests/test_recovery_policy.py`` for the pinned contract.

The policy is stateless. Instance state (name, future knobs) is set in
``__init__`` only.
"""

from __future__ import annotations

from pollypm.capacity import FAILOVER_TRIGGERS
from pollypm.recovery.base import (
    InterventionAction,
    InterventionHistoryEntry,
    RecoveryPolicy,
    SessionHealth,
    SessionSignals,
)


class DefaultRecoveryPolicy(RecoveryPolicy):
    """Default classifier + escalation ladder.

    Ladder:  nudge → reset → relaunch → failover → escalate

    The operator session is treated as a special case — it is allowed to
    sit idle or waiting on user input without triggering an intervention.
    """

    name = "default"

    # ── Classification ────────────────────────────────────────────────────

    def classify(self, signals: SessionSignals) -> SessionHealth:
        if not signals.window_present:
            return SessionHealth.EXITED

        if signals.pane_dead:
            return SessionHealth.EXITED

        if signals.auth_failure:
            return SessionHealth.AUTH_BROKEN

        if signals.capacity_state in FAILOVER_TRIGGERS:
            return SessionHealth.BLOCKED_NO_CAPACITY

        # Heuristic "blocked" verdict from the heartbeat LLM triage — the
        # session is waiting on a human and should not be bumped.
        if signals.last_verdict == "blocked":
            return SessionHealth.WAITING_ON_USER

        if signals.snapshot_repeated >= 3:
            return SessionHealth.LOOPING

        if signals.output_stale and signals.idle_cycles >= 3:
            return SessionHealth.STUCK

        if signals.output_stale:
            return SessionHealth.IDLE

        if signals.has_transcript_delta:
            return SessionHealth.ACTIVE

        return SessionHealth.HEALTHY

    # ── Intervention selection ────────────────────────────────────────────

    def select_intervention(
        self,
        health: SessionHealth,
        signals: SessionSignals,
        history: list[InterventionHistoryEntry],
    ) -> InterventionAction | None:
        previous_interventions = len(history)

        if health in (SessionHealth.ACTIVE, SessionHealth.HEALTHY):
            return None

        if health == SessionHealth.WAITING_ON_USER:
            # Workers asking "should I continue?" get nudged forward.
            # The operator can legitimately sit waiting for inbox direction.
            if signals.session_name == "operator":
                return None
            return InterventionAction(
                session_name=signals.session_name,
                action="nudge",
                reason="Session is waiting — triage will decide whether to push forward",
            )

        if health == SessionHealth.IDLE:
            # Prompt-idle operator is normal when there's no queue to process.
            if signals.session_name == "operator":
                return None
            if previous_interventions == 0:
                return InterventionAction(
                    session_name=signals.session_name,
                    action="nudge",
                    reason="Session has been idle, sending reminder",
                )
            elif previous_interventions == 1:
                return InterventionAction(
                    session_name=signals.session_name,
                    action="reset",
                    reason="Session remains idle after nudge",
                )
            else:
                return InterventionAction(
                    session_name=signals.session_name,
                    action="escalate",
                    reason="Session persistently idle after multiple interventions",
                )

        if health == SessionHealth.STUCK:
            if previous_interventions < 2:
                return InterventionAction(
                    session_name=signals.session_name,
                    action="reset",
                    reason="Session appears stuck with no progress",
                )
            else:
                return InterventionAction(
                    session_name=signals.session_name,
                    action="relaunch",
                    reason="Session stuck after reset attempts",
                )

        if health == SessionHealth.LOOPING:
            if previous_interventions < 2:
                return InterventionAction(
                    session_name=signals.session_name,
                    action="reset",
                    reason="Session is producing repeated identical output",
                )
            else:
                return InterventionAction(
                    session_name=signals.session_name,
                    action="relaunch",
                    reason="Session looping after reset attempts",
                )

        if health == SessionHealth.EXITED:
            return InterventionAction(
                session_name=signals.session_name,
                action="relaunch",
                reason="Session has exited unexpectedly",
            )

        if health == SessionHealth.AUTH_BROKEN:
            return InterventionAction(
                session_name=signals.session_name,
                action="failover",
                reason="Authentication failure detected",
            )

        if health == SessionHealth.BLOCKED_NO_CAPACITY:
            return InterventionAction(
                session_name=signals.session_name,
                action="failover",
                reason=f"Account capacity: {signals.capacity_state}",
            )

        if health == SessionHealth.ERROR:
            return InterventionAction(
                session_name=signals.session_name,
                action="escalate",
                reason="Session in error state",
            )

        return None
