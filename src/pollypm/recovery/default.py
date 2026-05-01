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

    # Time thresholds for work-service-aware classifications (#249).
    # 1800s (30min) mirrors the task_assignment notify dedupe window
    # so a stuck_on_task classification never out-races a resume ping.
    STUCK_ON_TASK_SECONDS = 1800
    SILENT_WORKER_SECONDS = 1800

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

        # #1010 — pane is showing a known idle-placeholder UI state
        # (Codex's rotating ``› <suggestion>`` hint, or Claude's empty
        # ``❯`` prompt). The agent is alive and waiting for input, NOT
        # stuck. Returning ``HEALTHY`` here short-circuits the
        # SILENT_WORKER / IDLE / STUCK / LOOPING ladders that would
        # otherwise keep raising ``stuck_session`` / ``silent_worker``
        # interventions and block #1008's 90s auto-clear streak.
        # Evaluated AFTER the hard mechanical failures (window gone,
        # pane dead, auth broken, capacity exhausted) so a placeholder
        # showing on top of a real failure doesn't mask it.
        if signals.pane_is_idle_placeholder:
            return SessionHealth.HEALTHY

        # Work-service-aware: the session is alive but hasn't made
        # progress on its claimed task for too long. Gate on turn_active
        # so a session actively generating output doesn't get flagged.
        # The pane being alive (not dead) is our liveness signal here.
        if (
            not signals.pane_dead
            and not signals.turn_active
            and signals.active_claim_task_id
            and signals.claim_age_seconds is not None
            and signals.claim_age_seconds > self.STUCK_ON_TASK_SECONDS
            and signals.last_event_seconds_ago is not None
            and signals.last_event_seconds_ago > self.STUCK_ON_TASK_SECONDS
        ):
            return SessionHealth.STUCK_ON_TASK

        # #296 — flow-state drift. Agent ended its turn, holds an active
        # claim, and the precomputed reconciliation action says the task
        # has observable deliverables past its current flow node. Evaluate
        # AFTER turn_active (a live turn shouldn't be flagged) and BEFORE
        # the generic IDLE classification so the drift alert takes
        # priority over routine-idle nudges.
        if (
            not signals.turn_active
            and signals.active_claim_task_id
            and signals.drift_action is not None
        ):
            return SessionHealth.STATE_DRIFT

        # Worker session is up but has no active claim and hasn't done
        # anything for 30min — it likely missed a queue event. Only
        # applies to "worker" role to avoid flagging operator/reviewer
        # sessions that legitimately idle.
        if (
            not signals.pane_dead
            and not signals.turn_active
            and signals.active_claim_task_id is None
            and signals.session_role == "worker"
            and signals.last_event_seconds_ago is not None
            and signals.last_event_seconds_ago > self.SILENT_WORKER_SECONDS
        ):
            return SessionHealth.SILENT_WORKER

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

        # ── #249 work-service-aware interventions ────────────────────────
        if health == SessionHealth.STUCK_ON_TASK:
            # Resume-ping via the task_assignment_notify plugin. The apply
            # side emits a TaskAssignmentEvent so the existing dedupe
            # table prevents double-firing, and raises a low-severity
            # stuck_on_task alert for operator visibility.
            return InterventionAction(
                session_name=signals.session_name,
                action="resume_ping",
                reason=(
                    f"Session idle on task {signals.active_claim_task_id} — "
                    f"claim age {signals.claim_age_seconds}s, "
                    f"last event {signals.last_event_seconds_ago}s ago"
                ),
                details={
                    "task_id": signals.active_claim_task_id,
                    "claim_age_seconds": signals.claim_age_seconds,
                    "last_event_seconds_ago": signals.last_event_seconds_ago,
                },
            )

        if health == SessionHealth.SILENT_WORKER:
            # Worker is up with no claim — send `pm task next` to kick
            # it into picking up the queue.
            return InterventionAction(
                session_name=signals.session_name,
                action="prompt_pm_task_next",
                reason=(
                    f"Worker idle without a claim — "
                    f"last event {signals.last_event_seconds_ago}s ago"
                ),
                details={
                    "last_event_seconds_ago": signals.last_event_seconds_ago,
                },
            )

        # #296 — observable flow-state drift. V1 policy: log + alert only.
        # We deliberately do NOT auto-advance the task node in v1; better
        # to flag than silently mutate state. The apply side owns the
        # event + alert writes (see ``work.progress_sweep``).
        if health == SessionHealth.STATE_DRIFT:
            action = signals.drift_action
            target = getattr(action, "advance_to_node", "") if action else ""
            reason = getattr(action, "reason", "") if action else ""
            return InterventionAction(
                session_name=signals.session_name,
                action="reconcile_flow_state",
                reason=(
                    f"Observable drift on task {signals.active_claim_task_id}: "
                    f"{reason}"
                ),
                details={
                    "task_id": signals.active_claim_task_id,
                    "advance_to_node": target,
                    "drift_reason": reason,
                },
            )

        return None
