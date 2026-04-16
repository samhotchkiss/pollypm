"""End-to-end heartbeat supervision loop.

Wires together signal collection, health classification, intervention
escalation, checkpoint recording, and capacity management into a
single supervision cycle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pollypm.capacity import (
    CapacityState,
    FAILOVER_TRIGGERS,
    can_failover_session,
    probe_capacity,
)
from pollypm.models import PollyPMConfig
from pollypm.storage.state import StateStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Health classifications
# ---------------------------------------------------------------------------


class SessionHealth(StrEnum):
    ACTIVE = "active"
    IDLE = "idle"
    STUCK = "stuck"
    LOOPING = "looping"
    EXITED = "exited"
    ERROR = "error"
    BLOCKED_NO_CAPACITY = "blocked_no_capacity"
    AUTH_BROKEN = "auth_broken"
    WAITING_ON_USER = "waiting_on_user"
    HEALTHY = "healthy"


# Intervention escalation ladder
INTERVENTION_LADDER = (
    "nudge",       # Send a reminder message to idle sessions
    "reset",       # Reset the session state
    "relaunch",    # Kill and relaunch the session
    "failover",    # Switch to a different account
    "escalate",    # Alert the operator for manual intervention
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SessionSignals:
    """Collected signals for a single session."""

    session_name: str
    window_present: bool = True
    pane_dead: bool = False
    output_stale: bool = False
    snapshot_repeated: int = 0  # Consecutive identical snapshots
    auth_failure: bool = False
    has_transcript_delta: bool = False
    capacity_state: CapacityState = CapacityState.UNKNOWN
    last_verdict: str = ""
    idle_cycles: int = 0


@dataclass(slots=True)
class InterventionAction:
    """An intervention to take for a session."""

    session_name: str
    action: str  # One of INTERVENTION_LADDER
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class HeartbeatCycleResult:
    """Result of a single heartbeat supervision cycle."""

    timestamp: str
    sessions_checked: int = 0
    classifications: dict[str, SessionHealth] = field(default_factory=dict)
    interventions: list[InterventionAction] = field(default_factory=list)
    checkpoints_recorded: int = 0
    alerts_raised: int = 0
    capacity_probes: int = 0


# ---------------------------------------------------------------------------
# Health classification
# ---------------------------------------------------------------------------


def classify_session_health(signals: SessionSignals) -> SessionHealth:
    """Classify session health from collected signals."""
    if not signals.window_present:
        return SessionHealth.EXITED

    if signals.pane_dead:
        return SessionHealth.EXITED

    if signals.auth_failure:
        return SessionHealth.AUTH_BROKEN

    if signals.capacity_state in FAILOVER_TRIGGERS:
        return SessionHealth.BLOCKED_NO_CAPACITY

    # Check if the session was classified as "blocked" (waiting on input)
    # by the heuristic classifier in _process_session
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


# ---------------------------------------------------------------------------
# Intervention selection
# ---------------------------------------------------------------------------


def select_intervention(
    health: SessionHealth,
    signals: SessionSignals,
    *,
    previous_interventions: int = 0,
) -> InterventionAction | None:
    """Select the appropriate intervention based on health and escalation state.

    Follows the intervention ladder:
    nudge → reset → relaunch → failover → escalate
    """
    if health in (SessionHealth.ACTIVE, SessionHealth.HEALTHY):
        return None

    if health == SessionHealth.WAITING_ON_USER:
        # Workers asking "should I continue?" should be pushed forward.
        # The heartbeat triage will use Haiku to decide the right action.
        # The operator can legitimately sit waiting for inbox/user direction.
        if signals.session_name == "operator":
            return None
        return InterventionAction(
            session_name=signals.session_name,
            action="nudge",
            reason="Session is waiting — triage will decide whether to push forward",
        )

    if health == SessionHealth.IDLE:
        # The operator being idle at the prompt is normal when there is no
        # queue to process. Avoid escalating prompt-idle control lanes.
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


# ---------------------------------------------------------------------------
# Heartbeat cycle
# ---------------------------------------------------------------------------


def run_heartbeat_cycle(
    config: PollyPMConfig,
    store: StateStore,
    *,
    session_signals: list[SessionSignals],
) -> HeartbeatCycleResult:
    """Run a single heartbeat supervision cycle.

    This is the core loop that:
    1. Classifies health for each session
    2. Checks capacity for affected accounts
    3. Selects interventions
    4. Records results

    Actual intervention execution (nudge, reset, relaunch, failover)
    is handled by the supervisor — this function determines WHAT to do.
    """
    result = HeartbeatCycleResult(
        timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        sessions_checked=len(session_signals),
    )

    for signals in session_signals:
        # Classify health
        health = classify_session_health(signals)
        result.classifications[signals.session_name] = health

        # Check if intervention is needed
        runtime = store.get_session_runtime(signals.session_name)
        previous_interventions = runtime.recovery_attempts if runtime else 0

        intervention = select_intervention(
            health, signals,
            previous_interventions=previous_interventions,
        )

        if intervention is not None:
            # Check lease before failover
            if intervention.action == "failover":
                can_fail, lease_reason = can_failover_session(store, signals.session_name)
                if not can_fail:
                    intervention = InterventionAction(
                        session_name=signals.session_name,
                        action="escalate",
                        reason=f"Failover blocked: {lease_reason}",
                    )

            result.interventions.append(intervention)
            result.alerts_raised += 1

        # Record checkpoint count
        result.checkpoints_recorded += 1

    return result


# ---------------------------------------------------------------------------
# Quick project state assessment
# ---------------------------------------------------------------------------


def assess_project_state(
    config: PollyPMConfig,
    store: StateStore,
    project_key: str,
) -> dict[str, Any]:
    """Quick project state assessment using checkpoint + issue tracker + overview."""
    state: dict[str, Any] = {
        "project_key": project_key,
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "sessions": {},
        "open_alerts": [],
        "capacity": {},
    }

    # Session states
    for session_name, session_config in config.sessions.items():
        if session_config.project != project_key and project_key != "pollypm":
            continue
        runtime = store.get_session_runtime(session_name)
        state["sessions"][session_name] = {
            "role": session_config.role,
            "status": runtime.status if runtime else "unknown",
            "account": session_config.account,
        }

    # Open alerts
    for alert in store.open_alerts():
        state["open_alerts"].append({
            "session": alert.session_name,
            "type": alert.alert_type,
            "severity": alert.severity,
            "message": alert.message,
        })

    # Capacity state
    for account_name in config.accounts:
        probe = probe_capacity(config, store, account_name)
        state["capacity"][account_name] = {
            "state": probe.state.value,
            "remaining_pct": probe.remaining_pct,
            "reason": probe.reason,
        }
        result = HeartbeatCycleResult(
            timestamp=state["timestamp"],
        )

    return state
