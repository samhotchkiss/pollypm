"""Recovery policy protocol and shared data types.

A ``RecoveryPolicy`` classifies session health from raw signals and
selects an intervention (or none) based on the health plus a history of
previous interventions. Policies make decisions only — the Supervisor
applies them (sending nudges, relaunching sessions, clearing alerts).

This module intentionally keeps the surface tiny:

  * :class:`SessionSignals` — the inputs a policy sees.
  * :class:`SessionHealth` — the classification output.
  * :class:`InterventionAction` — the decision output.
  * :class:`InterventionHistoryEntry` — per-session intervention history.
  * :class:`RecoveryPolicy` — the protocol plugins implement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from pollypm.capacity import CapacityState


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
    # #249 — work-service-aware classifications
    STUCK_ON_TASK = "stuck_on_task"
    SILENT_WORKER = "silent_worker"
    # #296 — observable-deliverables drift: the agent ended its turn
    # with plan/artefacts already on disk but the task's flow node
    # didn't advance. Alert + event only; no auto-advance in v1.
    STATE_DRIFT = "state_drift"


# Intervention escalation ladder
INTERVENTION_LADDER = (
    "nudge",       # Send a reminder message to idle sessions
    "reset",       # Reset the session state
    "relaunch",    # Kill and relaunch the session
    "failover",    # Switch to a different account
    "escalate",    # Alert the operator for manual intervention
)

# Extended interventions — work-service-aware actions (#249). These are
# not part of the sequential ladder above; they're action kinds emitted
# by the work-aware classifications and handled by the apply side.
EXTENDED_INTERVENTIONS = (
    "resume_ping",            # Re-emit a task_assignment notify event
    "prompt_pm_task_next",    # Send `pm task next` to the session
    "reconcile_flow_state",   # Log + alert observable flow drift (#296)
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
    # ── #249 work-service-aware signals ────────────────────────────────
    # These are optional — absent when the caller is a pure-mechanical
    # path (e.g. supervisor policy lookup from a failure type). Populated
    # by the session-health-sweep handler for each live session.
    active_claim_task_id: str | None = None
    claim_age_seconds: int | None = None
    last_event_seconds_ago: int | None = None
    last_commit_seconds_ago: int | None = None
    # Role hint — "worker", "operator", "reviewer", "pm-heartbeat", ...
    # Optional, defaults to "" for back-compat with older callers.
    session_role: str = ""
    # Whether an active turn indicator is visible in the pane. When True,
    # the session is currently processing — we should NOT classify as
    # stuck even if other timers have rolled over.
    turn_active: bool = False
    # #296 — precomputed drift-reconciliation action. When populated, the
    # classifier promotes the session to ``STATE_DRIFT`` instead of the
    # generic ``IDLE`` ladder. Carries the inferred target node + reason
    # so the intervention layer can log + alert without re-running the
    # heuristics. Type is ``ReconciliationAction | None`` — typed as
    # ``Any`` here to avoid a cycle with ``state_reconciliation``.
    drift_action: Any | None = None
    # #1010 — the pane is showing a known idle-placeholder UI state
    # (Codex rotating-suggestion hint or Claude empty ``❯`` prompt).
    # That's an alive-but-idle agent waiting for input, NOT a stuck
    # session — the classifier short-circuits to ``HEALTHY`` so the
    # heartbeat doesn't keep raising ``stuck_session`` /
    # ``silent_worker`` interventions against placeholder-showing
    # panes (which would block #1008's auto-clear streak).
    pane_is_idle_placeholder: bool = False


@dataclass(slots=True)
class InterventionAction:
    """An intervention to take for a session."""

    session_name: str
    action: str  # One of INTERVENTION_LADDER
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class InterventionHistoryEntry:
    """One prior intervention attempt for a session.

    The history is an ordered list (oldest first) of attempts the
    Supervisor has already made. Policies use this to walk the
    escalation ladder — e.g. nudge first, then reset, then relaunch.
    """

    action: str
    reason: str = ""
    at: str | None = None  # ISO-8601 timestamp (optional)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class RecoveryPolicy(Protocol):
    """Protocol for pluggable session-recovery decision makers.

    Implementations are pure — no state is owned by the policy, no side
    effects. The heartbeat loop / Supervisor calls ``classify`` then
    ``select_intervention`` and applies the returned action.
    """

    name: str

    def classify(self, signals: SessionSignals) -> SessionHealth:
        """Classify a session's health from collected signals."""
        ...

    def select_intervention(
        self,
        health: SessionHealth,
        signals: SessionSignals,
        history: list[InterventionHistoryEntry],
    ) -> InterventionAction | None:
        """Select the appropriate intervention, or ``None`` if nothing to do.

        ``history`` is the ordered list of prior interventions already
        attempted for this session. Policies walk the intervention ladder
        as the history grows.
        """
        ...
