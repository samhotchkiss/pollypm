"""Audit-log watchdog — pure detectors for orphan / stuck / leak patterns.

Born from the savethenovel post-mortem (2026-05-06): task #1 was
cancelled by Polly ~37s after start, leaving an orphan worker-marker
and an unpromoted draft #2 with zero cockpit affordance. The reaper
(#1341) cleans up stale markers on bootstrap, the audit log (#1342)
records every task / marker mutation, and *this* module is the
heartbeat consumer that scans those events to surface the same broken
state classes *before* a user notices.

The detectors are pure functions over a list of :class:`AuditEvent`
objects so unit tests can exercise each rule against a synthetic
fixture without spinning up the full heartbeat stack. The cadence
wiring lives in :mod:`pollypm.plugins_builtin.core_recurring.audit_watchdog`
which fans this detector out across every registered project, opens
the appropriate alert sink, and emits findings via ``upsert_alert``
(the existing surface used by ``blocked_chain.sweep`` and friends —
no new alert channel is introduced).

Detection rules (each returns a list of :class:`Finding`):

1. ``orphan_marker`` — ``marker.created`` for ``task-<project>-<N>``
   older than the configured threshold with no matching
   ``marker.released`` AND no ``task.status_changed`` to a terminal
   state (``done`` / ``cancelled`` / ``abandoned``). Catches the
   savethenovel pattern where a worker is launched and the task is
   then cancelled but the on-disk marker lingers.
2. ``marker_leaked`` — any ``marker.leaked`` event in the window.
   Each occurrence is a finding because the persona-swap-detected
   branch firing at all means a worker was redirected — we want
   visible signal on the leak rate, not silent log noise.
3. ``stuck_draft`` — ``task.created`` event from > ``stuck_draft_seconds``
   ago with no follow-up ``task.status_changed`` for the same subject
   to ``queued`` / ``in_progress`` / ``review``. (savethenovel/2's
   class — a planning draft that never got promoted and would only
   surface to the user via the empty-state affordance from #1340.)
4. ``cancellation_no_promotion`` — ``task.status_changed`` to
   ``cancelled`` with no ``task.created`` for the same project in the
   following ``cancel_grace_seconds`` window. Indicates a planning
   stall after a self-cancel (savethenovel/1's class — a worker
   self-cancels but Polly never queues a follow-up).
5. ``task_progress_stale`` — a live task currently at ``in_progress``
   whose worker has produced no transition / heartbeat / task-context
   activity for longer than ``progress_stale_seconds``. Catches the
   alive-but-unproductive worker class, including auth-blocked panes
   that keep accepting nudges but cannot make API calls.

Severity is ``"warn"`` for every rule today. The recovery hint is
attached to each finding so the alert message can include a
copy-pasteable next step (``"run pm task queue ..."``).

The watchdog itself emits a synthetic ``heartbeat.tick`` audit event
each time it runs so we can prove the heartbeat is alive (and
notice when it stops) — the cadence handler calls
:func:`emit_heartbeat_tick` after the scan.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from pollypm.audit.log import (
    EVENT_MARKER_CREATED,
    EVENT_MARKER_LEAKED,
    EVENT_MARKER_RELEASED,
    EVENT_TASK_CREATED,
    EVENT_TASK_STATUS_CHANGED,
    EVENT_WATCHDOG_ESCALATION_DISPATCHED,
    EVENT_WATCHDOG_OPERATOR_DISPATCHED,
    EVENT_WORKER_SESSION_REAPED,
    AuditEvent,
)


__all__ = [
    "Finding",
    "WatchdogConfig",
    "EVENT_HEARTBEAT_TICK",
    "EVENT_AUDIT_FINDING",
    "RULE_ORPHAN_MARKER",
    "RULE_MARKER_LEAKED",
    "RULE_STUCK_DRAFT",
    "RULE_CANCEL_NO_PROMOTION",
    "RULE_TASK_REVIEW_STALE",
    "RULE_TASK_PROGRESS_STALE",
    "RULE_ROLE_SESSION_MISSING",
    "RULE_WORKER_SESSION_DEAD_LOOP",
    "RULE_TASK_ON_HOLD_STALE",
    "RULE_DUPLICATE_ADVISOR_TASKS",
    "RULE_PLAN_REVIEW_MISSING",
    "RULE_LEGACY_DB_SHADOW",
    "RULE_PLAN_MISSING_ALERT_CHURN",
    "RULE_REJECTION_LOOP",
    "RULE_STATE_DB_MISSING",
    "RULE_QUEUE_WITHOUT_MOTION",
    "TIER_1",
    "TIER_2",
    "TIER_3",
    "TIER_4",
    "TIER_TERMINAL",
    "ON_HOLD_HUMAN_NEEDED_TAG",
    "ON_HOLD_ARCHITECT_TAG",
    "scan_events",
    "scan_project",
    "emit_heartbeat_tick",
    "emit_finding",
    "emit_escalation_dispatched",
    "emit_operator_dispatched",
    "format_unstick_brief",
    "tier_handoff_prompt",
    "build_urgent_human_handoff",
    "watchdog_alert_session_name",
    "WATCHDOG_ALERT_TYPE",
    "format_finding_message",
    "ESCALATION_THROTTLE_SECONDS",
    "OPERATOR_DISPATCH_THROTTLE_SECONDS",
    "REJECTION_LOOP_THRESHOLD",
    "REJECTION_LOOP_WINDOW_SECONDS",
    "QUEUE_MOTION_THRESHOLD_SECONDS",
]


logger = logging.getLogger(__name__)


# Stable rule names — pinned because alert dedupe uses them as part of
# the synthetic session_name key, and operators grep them in the audit
# log. New rules should follow ``noun.verb_or_state`` form.
RULE_ORPHAN_MARKER = "orphan_marker"
RULE_MARKER_LEAKED = "marker_leaked"
RULE_STUCK_DRAFT = "stuck_draft"
RULE_CANCEL_NO_PROMOTION = "cancellation_no_promotion"
# #1414 — auto-unstick rules. ``task_review_stale`` catches a task
# parked at status=review with no transitions for a while; that's the
# savethenovel/10 pattern (reviewer agent never spawned). The other
# two cover the architectural pattern: an in-flight task with no
# matching role tmux session, and a worker reaper firing in a loop on
# the same task.
RULE_TASK_REVIEW_STALE = "task_review_stale"
RULE_TASK_PROGRESS_STALE = "task_progress_stale"
RULE_ROLE_SESSION_MISSING = "role_session_missing"
RULE_WORKER_SESSION_DEAD_LOOP = "worker_session_dead_loop"
# #1424 — task_on_hold_stale catches the savethenovel/11 pattern where
# the reviewer transitions a task to status=on_hold (instead of reject),
# parking it for a human. The watchdog used to go silent on on_hold
# because ``task_review_stale`` only matches status=review. The new rule
# escalates to the architect by default — the architect re-assesses the
# reviewer's findings and either fixes them or, if the issue genuinely
# requires human judgement, calls ``pm notify`` itself.
RULE_TASK_ON_HOLD_STALE = "task_on_hold_stale"
# #1510 — duplicate_advisor_tasks catches a pile-up of non-terminal
# advisor-labeled tasks for the same project. The advisor.tick handler
# is supposed to enqueue at most one ``advisor_review`` per project at
# a time; if a tick race or an external write produces multiples, the
# self-healing path cancels every duplicate except the most recently
# created one. This rule is the detector — the cleanup runs from the
# cadence handler which holds a work-service handle.
RULE_DUPLICATE_ADVISOR_TASKS = "duplicate_advisor_tasks"
# #1511 — plan-review coverage gap. A plan-shaped task (label: ``poc-plan``
# / ``plan`` / ``project-plan``) reached ``done`` outside the
# ``plan_project`` flow, so the architect's reflection node (#1399) never
# fired and no ``plan_review`` inbox card was emitted. The watchdog
# backfills the missing card via the same code path the post-done hook
# uses; idempotent because both call sites probe the messages table for
# an existing plan_review row before writing.
RULE_PLAN_REVIEW_MISSING = "plan_review_missing"
# #1519 — legacy per-project DB shadows the canonical workspace DB. A
# project has rows in BOTH ``<workspace_root>/.pollypm/state.db`` AND
# ``<project>/.pollypm/state.db``; the dashboard's pipeline counts used
# to union them, double-counting any legacy rows. The read-path fix
# (#1519 Part A) prefers canonical, but the legacy file still wins
# whenever canonical is empty for the project — which is exactly the
# coffeeboardnm shape (1 canonical queued + 5 stale legacy queued).
# This detector finds the divergence so the cadence handler can drain
# the legacy DB via ``migrate_legacy_per_project_dbs`` (idempotent —
# rows already in canonical are skipped, source is archived to
# ``state.db.legacy-1004`` only after every row was either copied or
# matched).
RULE_LEGACY_DB_SHADOW = "legacy_db_shadow"
# #1524 — plan_missing alert churn. Part A's deterministic precompute
# in the task-assignment sweep should keep ``plan_missing`` alerts
# stable across consecutive ticks; if the same project still racks up
# multiple emit-clear pairs in a short window, something has regressed
# and the user is back to seeing the banner flash. The detector is the
# canary — it does not repair (Part A is the repair) — so future-us
# notices when the regression returns instead of the user noticing it
# in the cockpit. Threshold defaults to >=3 clear events on the same
# ``plan_gate-<project>`` scope inside ``plan_missing_churn_window_seconds``.
RULE_PLAN_MISSING_ALERT_CHURN = "plan_missing_alert_churn"
# #1546 — heartbeat-cascade foundation. Three new rules:
#
# * ``rejection_loop`` — fires when a task accumulates K=3+ consecutive
#   reviewer rejections at the same review node within a 2h window with
#   reject reasons that share a root-cause family (stem-overlap or
#   shared error-token signal). Tier-1 detector → tier-2 PM dispatch:
#   the worker can't escape the dependency-and-runtime context locally,
#   so the project PM receives structured evidence and decides between
#   retry-with-structural-change, restructure-the-work-itself, or
#   escalate-to-Polly. The detector deliberately does NOT auto-cancel.
# * ``state_db_missing`` — fires when a registered project has only
#   ``state.db.legacy-*`` archives in ``.pollypm/`` (the canonical
#   workspace DB was never created or got renamed away). Heals via
#   ``enable_tracked_project`` which re-runs the scaffold step.
# * ``queue_without_motion`` — generic safety-net probe: a project with
#   queued tasks and zero ``claim`` / execution-advance / status-change
#   events in the last ``queue_motion_threshold_seconds`` window (default
#   30 min). Fires as tier-2 with structured evidence (affected task IDs
#   + last-activity timestamps); the dispatcher routes it to the operator
#   leg today because the queue stalling out is a cross-cutting failure
#   that frequently outlives a tier-2 PM dispatch.
RULE_REJECTION_LOOP = "rejection_loop"
RULE_STATE_DB_MISSING = "state_db_missing"
RULE_QUEUE_WITHOUT_MOTION = "queue_without_motion"

# #1546 — tier classifications for the heartbeat cascade. Every Finding
# carries a ``tier`` so the dispatcher can route without re-reading the
# rule body:
#
# * ``TIER_1`` — self-heal. The watchdog itself fixes the symptom (e.g.
#   migrate legacy DB, backfill plan_review row, cancel duplicate
#   advisor tasks, spawn missing role lane). Idempotent across ticks.
# * ``TIER_2`` — PM/architect dispatch. The watchdog hands evidence to
#   the project's architect tmux pane via send-keys; the architect
#   decides what to do.
# * ``TIER_3`` — operator/human dispatch. The watchdog routes to the
#   user's inbox via the ``pm notify``-shaped path. Used when tier-2
#   has been tried (or is structurally unavailable) and the next rung
#   is a human.
# * ``TIER_4`` — broadened-Polly authority. Reserved for follow-up
#   issues; today no rule emits at this tier.
# * ``TIER_TERMINAL`` — observe-only. The rule produces a finding for
#   forensic / alerting purposes but no automated action follows. Used
#   for orphan_marker, marker_leaked, cancellation_no_promotion,
#   plan_missing_alert_churn (canary detectors).
TIER_1 = "1"
TIER_2 = "2"
TIER_3 = "3"
TIER_4 = "4"
TIER_TERMINAL = "terminal"

# Reason-string tags reviewers use to hint the watchdog routing. The
# default (no tag) is architect-actionable. ``ON_HOLD_HUMAN_NEEDED_TAG``
# is reserved for cases that genuinely need a product/taste call from
# the user; the dispatch path can escalate to user directly when it
# spots this prefix. The strings are matched case-insensitively against
# the leading characters of the on_hold transition reason.
ON_HOLD_ARCHITECT_TAG = "architect-actionable"
ON_HOLD_HUMAN_NEEDED_TAG = "human-needed"

# Throttle window for the dispatch path. We re-fire findings on every
# tick (the audit log is forensic and operators want repeat counts),
# but we deliberately do NOT re-dispatch the architect for the same
# (project, finding_type, subject) within 30 min. The dispatch event
# itself is the source of truth — querying the audit log avoids any
# in-memory state that wouldn't survive a heartbeat process restart.
ESCALATION_THROTTLE_SECONDS = 1800
# #1546 — operator-leg throttle. Tier-3 dispatch is more disruptive than
# tier-2 (it surfaces a card in the user's cockpit inbox) so the cooldown
# is wider — same hour-long window plus 30 min headroom. The throttle is
# enforced by querying the audit log for
# ``EVENT_WATCHDOG_OPERATOR_DISPATCHED`` rows on (rule, project, subject)
# tuples — same idempotence model as the architect leg.
OPERATOR_DISPATCH_THROTTLE_SECONDS = 5400
# #1546 — rejection-loop detector tunables. Three consecutive rejections
# at the same review node inside two hours, with reject reasons that
# cluster, is the worker-can't-escape-locally signal. Tunable via the
# ``WatchdogConfig`` so future-us can experiment without touching the
# detector body.
REJECTION_LOOP_THRESHOLD = 3
REJECTION_LOOP_WINDOW_SECONDS = 7200
# #1546 — queue-without-motion probe threshold. A project with queued
# tasks and zero claim / execution / status-change activity for this
# long is treated as a stalled queue. 30 min is intentionally shorter
# than ``ESCALATION_THROTTLE_SECONDS`` so the probe lands on the same
# cadence as the architect throttle window expires — a queue that's
# been silent for 30 min has already missed several heartbeat ticks.
QUEUE_MOTION_THRESHOLD_SECONDS = 1800

# Audit events emitted *by* the watchdog itself.
EVENT_HEARTBEAT_TICK = "heartbeat.tick"
EVENT_AUDIT_FINDING = "audit.finding"

# Alert type used for ``upsert_alert`` — single type for all watchdog
# rules; the rule name lands in the message body and metadata so the
# operator can still tell them apart without us inventing four new
# alert types.
WATCHDOG_ALERT_TYPE = "audit_watchdog"

# Terminal task states — a transition into any of these means the
# task is no longer running, which closes out an orphan-marker check.
_TERMINAL_TASK_STATES: frozenset[str] = frozenset({
    "done", "cancelled", "abandoned",
})

# Promotion-out-of-draft states — when a draft transitions to any of
# these, we treat it as no longer "stuck".
_DRAFT_PROMOTION_STATES: frozenset[str] = frozenset({
    "queued", "in_progress", "review", "blocked", "rework",
})

# Marker filename pattern: ``task-<project>-<N>.<kind>`` (typically
# ``.fresh``). Captures the project and task_number so the orphan
# check can correlate the marker with task.status_changed events.
_MARKER_NAME_RE = re.compile(
    r"task-(?P<project>[^/\\]+?)-(?P<num>\d+)(?:\.\w+)?$"
)


@dataclass(slots=True, frozen=True)
class WatchdogConfig:
    """Tunable thresholds for watchdog detection rules.

    All durations are in seconds. Defaults match the spec in the
    task brief — 30 min orphan age threshold / leak lookback, 5 min
    for the cheaper draft / cancellation gates.
    """

    # Age threshold for orphan-marker detection, and lookback for
    # marker-leak detection.
    window_seconds: int = 1800
    # A draft must have existed this long with no promotion before
    # it counts as stuck. Short enough to catch the savethenovel
    # pattern (planning queue stalled mid-flight) without false
    # positives during normal multi-step plan generation.
    stuck_draft_seconds: int = 300
    # After a cancel, this long is the grace window for Polly to
    # queue the replacement. If no ``task.created`` for the same
    # project lands in that window, fire the finding.
    cancel_grace_seconds: int = 300
    # #1414 — task_review_stale: a task at status=review must have its
    # most recent transition older than this before we fire. 30 min
    # mirrors the savethenovel/10 case (reviewer agent never spawned).
    review_stale_seconds: int = 1800
    # #1444 — task_progress_stale: a task at status=in_progress with no
    # transition / worker heartbeat / task-context activity for this
    # long is treated as a silent worker stall. 20 min is intentionally
    # shorter than review stale because an implementing worker should
    # either advance the task, emit progress context, or heartbeat.
    progress_stale_seconds: int = 1200
    # #1424 — task_on_hold_stale: a task at status=on_hold for longer
    # than this triggers the architect-first escalation. Defaults to
    # 15 min — shorter than ``review_stale_seconds`` because on_hold is
    # supposed to be a brief routing detour (reviewer can't decide,
    # parks task), not a sustained state. Anything longer than the
    # cadence-tick stride means a human is now load-bearing.
    on_hold_stale_seconds: int = 900
    # #1414 — worker_session_dead_loop: how many reaper events for the
    # same task within ``dead_loop_window_seconds`` count as a loop.
    dead_loop_threshold: int = 3
    dead_loop_window_seconds: int = 600
    # #1511 — plan_review_missing: how far back to look for plan-shaped
    # ``done`` tasks when scanning for a missing plan_review row. 14 days
    # is wide enough to catch plans the user genuinely hasn't reviewed
    # yet without re-firing forever on long-since-abandoned drafts.
    plan_review_lookback_seconds: int = 14 * 24 * 60 * 60
    # #1524 — plan_missing alert churn. Window + threshold for the
    # canary detector that fires when the sweep handler is bouncing
    # the same plan_missing alert open/closed across consecutive ticks
    # (the regression Part A repairs). 10 min covers ~12 sweep ticks
    # at the @every 50s cadence; 3 clears in that window is well above
    # the steady-state of zero (Part A means the alert refreshes in
    # place, never closes-and-reopens).
    plan_missing_churn_window_seconds: int = 600
    plan_missing_churn_threshold: int = 3
    # #1546 — rejection-loop detector. K=3 consecutive rejections at
    # the same review node inside W=2h with clustered reject reasons.
    rejection_loop_threshold: int = REJECTION_LOOP_THRESHOLD
    rejection_loop_window_seconds: int = REJECTION_LOOP_WINDOW_SECONDS
    # #1546 — queue-without-motion safety-net probe. A project with
    # queued tasks and no claim / execution / status-change activity
    # for this many seconds fires a tier-2 finding routed to the
    # operator leg.
    queue_motion_threshold_seconds: int = QUEUE_MOTION_THRESHOLD_SECONDS


@dataclass(slots=True, frozen=True)
class Finding:
    """One watchdog detection — what was wrong, where, and what to do.

    ``rule`` is one of the ``RULE_*`` constants. ``project`` and
    ``subject`` mirror the audit-event shape so a downstream alert
    can scope itself the same way other PollyPM alerts do.
    ``recommendation`` is a copy-pasteable hint (CLI command, file
    pointer, etc.) — surfaced to the user verbatim.

    #1546 — ``tier`` classifies the finding for the heartbeat-cascade
    dispatcher. See the ``TIER_*`` module constants for values; the
    dispatcher uses this field to decide between self-heal, architect
    dispatch, and operator dispatch without re-reading the rule body.
    Defaults to :data:`TIER_TERMINAL` so a rule that forgets to set
    the field stays observe-only (the safe default).
    ``evidence`` is the structured payload for tier-2/3/4 prompt
    builders — what was tried, what failed, the pattern across them.
    Distinct from ``metadata`` (free-form telemetry) because the
    cascade prompt builders consume ``evidence`` directly and refuse
    to read free-form fields. Optional; rules that don't need
    evidence-driven escalation can leave it empty.
    """

    rule: str
    project: str
    subject: str
    severity: str = "warn"
    message: str = ""
    recommendation: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    tier: str = TIER_TERMINAL
    evidence: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_iso(stamp: str) -> datetime | None:
    """Parse an ISO-8601 timestamp string into a tz-aware datetime."""
    if not stamp:
        return None
    try:
        # ``datetime.fromisoformat`` handles the ``+00:00`` suffix our
        # writer emits. Earlier Pythons choked on ``Z`` but 3.11+ does
        # not — and we target 3.11+ everywhere.
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _coerce_datetime(value: Any) -> datetime | None:
    """Coerce datetime-ish values from models / events to tz-aware UTC."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        return _parse_iso(value)
    return None


def _marker_task_key(subject: str) -> tuple[str, int] | None:
    """Extract ``(project, task_number)`` from a marker subject path.

    Marker subjects are absolute paths like
    ``/.../<root>/.pollypm/worker-markers/task-savethenovel-1.fresh``.
    Returns ``None`` for marker filenames we don't recognise — those
    are never task-bound markers (e.g. future ``advisor-*`` markers)
    and we skip them rather than firing spurious findings.
    """
    if not subject:
        return None
    name = Path(subject).name
    match = _MARKER_NAME_RE.match(name)
    if match is None:
        return None
    project = match.group("project")
    try:
        num = int(match.group("num"))
    except ValueError:
        return None
    return (project, num)


def _task_subject_key(subject: str) -> tuple[str, int] | None:
    """Parse ``project/N`` task subject into ``(project, N)``."""
    if not subject or "/" not in subject:
        return None
    project, _, num_s = subject.rpartition("/")
    try:
        return (project, int(num_s))
    except ValueError:
        return None


def watchdog_alert_session_name(rule: str, project: str, subject: str) -> str:
    """Synthetic session name for ``upsert_alert`` dedupe.

    Mirrors :func:`pollypm.plugins_builtin.core_recurring.blocked_chain
    .blocked_dead_end_session_name` — one row per (rule, project,
    subject) so repeated ticks refresh the same alert instead of
    spawning duplicates.
    """
    safe_subject = subject.replace("/", "_").replace(" ", "_") or "_"
    return f"audit-{rule}-{project}-{safe_subject}"


def format_finding_message(finding: Finding) -> str:
    """Render a Finding as the alert message body."""
    parts = [finding.message or finding.rule]
    if finding.recommendation:
        parts.append(f"Recommendation: {finding.recommendation}")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Rule implementations — each returns ``list[Finding]``
# ---------------------------------------------------------------------------


def _detect_orphan_markers(
    events: Sequence[AuditEvent],
    *,
    now: datetime,
    config: WatchdogConfig,
) -> list[Finding]:
    """Rule 1: ``marker.created`` with no release + no terminal transition.

    Walks the windowed event list once. For each ``marker.created``
    older than ``window_seconds`` we extract the (project, task_number)
    and check whether either a ``marker.released`` for the same marker
    subject *or* a ``task.status_changed`` to a terminal state for the
    same task appears in the scanned range. If neither, it's an orphan.
    """
    findings: list[Finding] = []
    cutoff = now - timedelta(seconds=config.window_seconds)

    # Collect created markers that have crossed the orphan threshold.
    created: list[tuple[AuditEvent, tuple[str, int]]] = []
    released_subjects: set[str] = set()
    terminal_tasks: set[tuple[str, int]] = set()

    for ev in events:
        ts = _parse_iso(ev.ts)
        if ts is None:
            continue
        if ev.event == EVENT_MARKER_CREATED and ev.status == "ok":
            if ts > cutoff:
                continue
            key = _marker_task_key(ev.subject)
            if key is not None:
                created.append((ev, key))
        elif ev.event == EVENT_MARKER_RELEASED:
            released_subjects.add(ev.subject)
        elif ev.event == EVENT_TASK_STATUS_CHANGED:
            to_state = (ev.metadata or {}).get("to")
            if isinstance(to_state, str) and to_state in _TERMINAL_TASK_STATES:
                key = _task_subject_key(ev.subject)
                if key is not None:
                    terminal_tasks.add(key)

    for ev, key in created:
        if ev.subject in released_subjects:
            continue
        if key in terminal_tasks:
            # Task transitioned to a terminal state — the reaper /
            # cleanup paths will handle the marker; don't double-warn.
            continue
        project, task_number = key
        findings.append(Finding(
            rule=RULE_ORPHAN_MARKER,
            tier=TIER_TERMINAL,
            project=project,
            subject=f"{project}/{task_number}",
            message=(
                f"Worker marker for task {project}/{task_number} has "
                f"been alive for >{config.window_seconds // 60} min "
                f"with no release and no terminal task transition."
            ),
            recommendation=(
                f"Inspect the worker pane (window task-{project}-"
                f"{task_number}) and either resume or run "
                f"`pm task cancel {project}/{task_number}` to release "
                f"the marker."
            ),
            metadata={
                "marker_subject": ev.subject,
                "created_at": ev.ts,
                "actor": ev.actor,
            },
        ))
    return findings


def _detect_marker_leaks(
    events: Sequence[AuditEvent],
    *,
    now: datetime,
    config: WatchdogConfig,
) -> list[Finding]:
    """Rule 2: any ``marker.leaked`` event in the window.

    The persona-swap-detected branch only emits this when a worker
    was redirected mid-launch. Every occurrence is worth surfacing —
    we want a visible leak counter, not silent log noise.
    """
    findings: list[Finding] = []
    cutoff = now - timedelta(seconds=config.window_seconds)
    for ev in events:
        if ev.event != EVENT_MARKER_LEAKED:
            continue
        ts = _parse_iso(ev.ts)
        if ts is None or ts < cutoff:
            continue
        key = _marker_task_key(ev.subject)
        if key is not None:
            project, task_number = key
            subject = f"{project}/{task_number}"
        else:
            project = ev.project
            subject = ev.subject or "<unknown>"
        findings.append(Finding(
            rule=RULE_MARKER_LEAKED,
            tier=TIER_TERMINAL,
            project=project,
            subject=subject,
            message=(
                f"Marker leak detected at {ev.ts} (persona-swap path). "
                f"A worker pane was redirected before its marker could "
                f"be released."
            ),
            recommendation=(
                "Investigate session_services/tmux.py "
                "persona_swap_detected branch — this should be rare; "
                "repeat occurrences point at a tmux session-name "
                "collision."
            ),
            metadata={
                "marker_subject": ev.subject,
                "leaked_at": ev.ts,
                "actor": ev.actor,
                **dict(ev.metadata or {}),
            },
        ))
    return findings


def _detect_stuck_drafts(
    events: Sequence[AuditEvent],
    *,
    now: datetime,
    config: WatchdogConfig,
    open_tasks: Sequence[Any] | None = None,
) -> list[Finding]:
    """Rule 3 (state-based, #1433): tasks currently at ``status=draft``
    older than ``stuck_draft_seconds``.

    Primary detection path queries the live ``work_tasks`` view via
    ``open_tasks`` — any task currently at ``status=draft`` whose
    ``created_at`` is older than the threshold is stuck. The audit-event
    path still fires for synthetic-fixture tests (and as a safety net
    when ``open_tasks`` isn't supplied), so old behavior is preserved.

    Findings produced by the state path take precedence — we dedupe
    on ``(project, subject)`` so a task that is both visible in
    ``open_tasks`` AND has a matching audit event only fires once.
    """
    findings: list[Finding] = []
    seen_subjects: set[str] = set()
    cutoff = now - timedelta(seconds=config.stuck_draft_seconds)

    # State-based path (#1433): walks current ``work_tasks`` rows.
    if open_tasks:
        for task in open_tasks:
            status = getattr(task, "work_status", None)
            status_value = getattr(status, "value", status)
            if status_value != "draft":
                continue
            created_at = getattr(task, "created_at", None)
            if created_at is None:
                continue
            if getattr(created_at, "tzinfo", None) is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            if created_at > cutoff:
                continue
            project = getattr(task, "project", "") or ""
            task_number = getattr(task, "task_number", None)
            if not project or task_number is None:
                continue
            subject = f"{project}/{task_number}"
            if subject in seen_subjects:
                continue
            seen_subjects.add(subject)
            actor = getattr(task, "created_by", "") or "unknown"
            findings.append(Finding(
                rule=RULE_STUCK_DRAFT,
                tier=TIER_2,
                project=project,
                subject=subject,
                message=(
                    f"Draft task {subject} has sat unpromoted for "
                    f">{config.stuck_draft_seconds // 60} min "
                    f"(created {created_at.isoformat()})."
                ),
                recommendation=(
                    f"Promote with `pm task queue {subject}` or "
                    f"discard with `pm task cancel {subject}`. "
                    f"Originating actor: {actor}."
                ),
                metadata={
                    "created_at": created_at.isoformat(),
                    "actor": actor,
                    "title": getattr(task, "title", None),
                    "detected_via": "state",
                },
            ))

    # Event-based fallback (backward-compat with old fixtures).
    promoted_subjects: set[str] = set()
    created_events: list[AuditEvent] = []
    for ev in events:
        if ev.event == EVENT_TASK_STATUS_CHANGED:
            to_state = (ev.metadata or {}).get("to")
            if isinstance(to_state, str) and (
                to_state in _DRAFT_PROMOTION_STATES
                or to_state in _TERMINAL_TASK_STATES
            ):
                promoted_subjects.add(ev.subject)
        elif ev.event == EVENT_TASK_CREATED:
            ts = _parse_iso(ev.ts)
            if ts is None or ts > cutoff:
                # Too recent — give it grace.
                continue
            created_events.append(ev)

    for ev in created_events:
        if ev.subject in promoted_subjects:
            continue
        if ev.subject in seen_subjects:
            continue
        key = _task_subject_key(ev.subject)
        if key is None:
            continue
        project, task_number = key
        seen_subjects.add(ev.subject)
        findings.append(Finding(
            rule=RULE_STUCK_DRAFT,
            tier=TIER_2,
            project=project,
            subject=ev.subject,
            message=(
                f"Draft task {ev.subject} has sat unpromoted for "
                f">{config.stuck_draft_seconds // 60} min "
                f"(created {ev.ts})."
            ),
            recommendation=(
                f"Promote with `pm task queue {ev.subject}` or "
                f"discard with `pm task cancel {ev.subject}`. "
                f"Originating actor: {ev.actor or 'unknown'}."
            ),
            metadata={
                "created_at": ev.ts,
                "actor": ev.actor,
                "title": (ev.metadata or {}).get("title"),
                "detected_via": "event",
            },
        ))
    return findings


def _detect_cancellation_no_promotion(
    events: Sequence[AuditEvent],
    *,
    now: datetime,
    config: WatchdogConfig,
) -> list[Finding]:
    """Rule 4: cancel without follow-up create within the grace window.

    The savethenovel/1 pattern: Polly cancels a task, then never
    queues the replacement. Detection: for each ``task.status_changed``
    to ``cancelled`` whose timestamp lies in
    ``[now - window, now - cancel_grace_seconds]`` (i.e. the grace
    window has fully elapsed), check whether any ``task.created`` for
    the same project exists with a strictly later timestamp. If not,
    fire.
    """
    findings: list[Finding] = []
    grace = timedelta(seconds=config.cancel_grace_seconds)
    window_start = now - timedelta(seconds=config.window_seconds)
    grace_cutoff = now - grace

    # Map project -> sorted list of task.created timestamps.
    created_by_project: dict[str, list[datetime]] = {}
    cancellations: list[tuple[AuditEvent, datetime]] = []
    for ev in events:
        ts = _parse_iso(ev.ts)
        if ts is None:
            continue
        if ev.event == EVENT_TASK_CREATED and ev.project:
            created_by_project.setdefault(ev.project, []).append(ts)
        elif ev.event == EVENT_TASK_STATUS_CHANGED:
            to_state = (ev.metadata or {}).get("to")
            if to_state == "cancelled" and ts >= window_start and ts <= grace_cutoff:
                cancellations.append((ev, ts))

    for ev, ts in cancellations:
        creates = created_by_project.get(ev.project, [])
        # Was there *any* task.created for this project after the
        # cancellation? Even one means Polly kept the queue moving.
        followed = any(c > ts for c in creates)
        if followed:
            continue
        findings.append(Finding(
            rule=RULE_CANCEL_NO_PROMOTION,
            tier=TIER_TERMINAL,
            project=ev.project,
            subject=ev.subject,
            message=(
                f"Task {ev.subject} was cancelled at {ev.ts} but no "
                f"replacement task.created landed in the next "
                f"{config.cancel_grace_seconds // 60} min. The "
                f"planning queue may have stalled."
            ),
            recommendation=(
                f"Open the project drilldown and check Polly's "
                f"planning state, or run `pm chat {ev.project}` and "
                f"prompt 'what's next?' to nudge the queue."
            ),
            metadata={
                "cancelled_at": ev.ts,
                "actor": ev.actor,
                "from": (ev.metadata or {}).get("from"),
            },
        ))
    return findings


# ---------------------------------------------------------------------------
# Auto-unstick rules (#1414)
# ---------------------------------------------------------------------------


def _state_entry_time(
    task: Any, target_state: str,
) -> tuple[datetime | None, dict[str, Any]]:
    """Return ``(entry_ts, transition_meta)`` for ``task`` entering ``target_state``.

    Walks ``task.transitions`` (newest first by timestamp) and returns
    the timestamp of the most recent transition whose ``to_state``
    matches. ``transition_meta`` carries ``from``, ``actor``, and
    ``reason`` so callers can fold them into the Finding metadata
    without re-walking.

    When the task carries no transition history (e.g. the
    ``include_history=False`` path of :class:`SQLiteWorkService`),
    falls back to ``task.updated_at`` and finally ``task.created_at``.
    Either fallback is best-effort but yields a still-useful "stuck
    since" signal — the threshold check still gates the finding.
    """
    transitions = list(getattr(task, "transitions", None) or [])
    matching: list[Any] = []
    for tr in transitions:
        to_state = getattr(tr, "to_state", None)
        if to_state == target_state:
            matching.append(tr)
    if matching:
        matching.sort(
            key=lambda tr: getattr(tr, "timestamp", None) or datetime.min,
            reverse=True,
        )
        latest = matching[0]
        ts = getattr(latest, "timestamp", None)
        if isinstance(ts, datetime):
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts, {
                "from": getattr(latest, "from_state", None),
                "actor": getattr(latest, "actor", None),
                "reason": getattr(latest, "reason", None),
            }
    # Fallback: ``updated_at`` is touched on every status change, so
    # for a task currently *at* ``target_state`` it's a reasonable
    # proxy for state-entry time. ``created_at`` is the last-resort.
    fallback = getattr(task, "updated_at", None) or getattr(
        task, "created_at", None,
    )
    if isinstance(fallback, datetime):
        if fallback.tzinfo is None:
            fallback = fallback.replace(tzinfo=timezone.utc)
        return fallback, {"from": None, "actor": None, "reason": None}
    return None, {"from": None, "actor": None, "reason": None}


def _detect_task_review_stale(
    events: Sequence[AuditEvent],
    *,
    now: datetime,
    config: WatchdogConfig,
    open_tasks: Sequence[Any] | None = None,
) -> list[Finding]:
    """Rule 5 (state-based, #1433): tasks currently at ``status=review``
    whose entry-into-review is older than ``review_stale_seconds``.

    Primary path queries the live ``work_tasks`` view (any task at
    ``status=review``) and computes "how long in this state" from the
    most recent ``review`` transition (``task.transitions``) or
    falls back to ``updated_at`` / ``created_at``. This catches tasks
    that entered ``review`` *before* the audit-log scan window and
    would otherwise be invisible to the watchdog.

    Audit-event scan is preserved as a fallback so synthetic-fixture
    tests (and event-only callers) keep firing. Findings dedupe on
    ``subject`` so we never report the same task twice in one tick.
    """
    findings: list[Finding] = []
    seen_subjects: set[str] = set()
    cutoff = now - timedelta(seconds=config.review_stale_seconds)

    # State-based path (#1433): walks current ``work_tasks`` rows.
    if open_tasks:
        for task in open_tasks:
            status = getattr(task, "work_status", None)
            status_value = getattr(status, "value", status)
            if status_value != "review":
                continue
            project = getattr(task, "project", "") or ""
            task_number = getattr(task, "task_number", None)
            if not project or task_number is None:
                continue
            subject = f"{project}/{task_number}"
            entry_ts, tr_meta = _state_entry_time(task, "review")
            if entry_ts is None:
                continue
            if entry_ts > cutoff:
                continue
            stuck_minutes = max(1, int((now - entry_ts).total_seconds() // 60))
            seen_subjects.add(subject)
            findings.append(Finding(
                rule=RULE_TASK_REVIEW_STALE,
                tier=TIER_2,
                project=project,
                subject=subject,
                message=(
                    f"Task {subject} has been at status=review for "
                    f"~{stuck_minutes} min with no further transitions."
                ),
                recommendation=(
                    f"Either spawn a reviewer or run `pm task done "
                    f"{subject}` if the work is correct as-is."
                ),
                metadata={
                    "review_since": entry_ts.isoformat(),
                    "stuck_minutes": stuck_minutes,
                    "from": tr_meta.get("from"),
                    "actor": tr_meta.get("actor"),
                    "detected_via": "state",
                },
            ))

    # Event-based fallback path (backward-compat with old fixtures).
    latest: dict[str, tuple[datetime, AuditEvent]] = {}
    for ev in events:
        if ev.event != EVENT_TASK_STATUS_CHANGED:
            continue
        ts = _parse_iso(ev.ts)
        if ts is None:
            continue
        prior = latest.get(ev.subject)
        if prior is None or ts > prior[0]:
            latest[ev.subject] = (ts, ev)

    for subject, (ts, ev) in latest.items():
        if subject in seen_subjects:
            continue
        to_state = (ev.metadata or {}).get("to")
        if to_state != "review":
            continue
        if ts > cutoff:
            continue
        key = _task_subject_key(subject)
        if key is None:
            continue
        project, _ = key
        stuck_minutes = max(1, int((now - ts).total_seconds() // 60))
        seen_subjects.add(subject)
        findings.append(Finding(
            rule=RULE_TASK_REVIEW_STALE,
            tier=TIER_2,
            project=project,
            subject=subject,
            message=(
                f"Task {subject} has been at status=review for "
                f"~{stuck_minutes} min with no further transitions."
            ),
            recommendation=(
                f"Either spawn a reviewer or run `pm task done "
                f"{subject}` if the work is correct as-is."
            ),
            metadata={
                "review_since": ev.ts,
                "stuck_minutes": stuck_minutes,
                "from": (ev.metadata or {}).get("from"),
                "actor": ev.actor,
                "detected_via": "event",
            },
        ))
    return findings


def _classify_on_hold_reason(reason: str | None) -> str:
    """Return ``architect-actionable`` or ``human-needed`` based on the reason.

    Reviewers tag on_hold transitions with a leading routing hint so the
    watchdog can decide whether the architect or the user should be the
    first responder. The match is case-insensitive and tolerant of
    surrounding punctuation (``"[human-needed] copy approval"`` works).
    Default routing is architect-actionable because that's what #1424
    sets as the post-mortem default — the architect can always escalate
    upward, but parking on the human is the failure mode.
    """
    if not reason:
        return ON_HOLD_ARCHITECT_TAG
    head = reason.strip().lower()
    if head.startswith(ON_HOLD_HUMAN_NEEDED_TAG.lower()):
        return ON_HOLD_HUMAN_NEEDED_TAG
    # ``[architect-actionable] ...`` and bare ``architect-actionable: ...``
    # both collapse to the architect default. Anything else also defaults
    # to architect — the reviewer just didn't tag it.
    if head.startswith("[" + ON_HOLD_HUMAN_NEEDED_TAG.lower()):
        return ON_HOLD_HUMAN_NEEDED_TAG
    return ON_HOLD_ARCHITECT_TAG


def _detect_task_on_hold_stale(
    events: Sequence[AuditEvent],
    *,
    now: datetime,
    config: WatchdogConfig,
    open_tasks: Sequence[Any] | None = None,
) -> list[Finding]:
    """Rule 5b (state-based, #1424 + #1433): tasks currently at
    ``status=on_hold`` longer than ``on_hold_stale_seconds``.

    Primary path queries the live ``work_tasks`` view; fires for any
    task currently at ``on_hold`` whose entry-into-on_hold (from
    ``task.transitions`` or ``updated_at`` fallback) is older than the
    threshold. This catches tasks that entered ``on_hold`` *before*
    the audit-log scan window — savethenovel/11's class.

    Audit-event scan is preserved for synthetic-fixture tests and as
    a safety net. Findings dedupe on ``subject``.
    """
    findings: list[Finding] = []
    seen_subjects: set[str] = set()
    cutoff = now - timedelta(seconds=config.on_hold_stale_seconds)

    # State-based path (#1433): walks current ``work_tasks`` rows.
    if open_tasks:
        for task in open_tasks:
            status = getattr(task, "work_status", None)
            status_value = getattr(status, "value", status)
            if status_value != "on_hold":
                continue
            project = getattr(task, "project", "") or ""
            task_number = getattr(task, "task_number", None)
            if not project or task_number is None:
                continue
            subject = f"{project}/{task_number}"
            entry_ts, tr_meta = _state_entry_time(task, "on_hold")
            if entry_ts is None:
                continue
            if entry_ts > cutoff:
                continue
            stuck_minutes = max(1, int((now - entry_ts).total_seconds() // 60))
            reason = tr_meta.get("reason")
            routing = _classify_on_hold_reason(
                reason if isinstance(reason, str) else None,
            )
            seen_subjects.add(subject)
            findings.append(Finding(
                rule=RULE_TASK_ON_HOLD_STALE,
                tier=TIER_2,
                project=project,
                subject=subject,
                message=(
                    f"Task {subject} has been at status=on_hold for "
                    f"~{stuck_minutes} min — escalating to architect for "
                    f"first-responder unstick."
                ),
                recommendation=(
                    f"Architect: re-read the reviewer's rationale, fix the "
                    f"issue, then `pm task queue {subject}`. Only escalate "
                    f"to user via `pm notify` if the issue genuinely needs "
                    f"human judgement."
                ),
                metadata={
                    "on_hold_since": entry_ts.isoformat(),
                    "stuck_minutes": stuck_minutes,
                    "from": tr_meta.get("from"),
                    "reason": reason if isinstance(reason, str) else None,
                    "routing": routing,
                    "actor": tr_meta.get("actor"),
                    "detected_via": "state",
                },
            ))

    # Event-based fallback path (backward-compat with old fixtures).
    latest: dict[str, tuple[datetime, AuditEvent]] = {}
    for ev in events:
        if ev.event != EVENT_TASK_STATUS_CHANGED:
            continue
        ts = _parse_iso(ev.ts)
        if ts is None:
            continue
        prior = latest.get(ev.subject)
        if prior is None or ts > prior[0]:
            latest[ev.subject] = (ts, ev)

    for subject, (ts, ev) in latest.items():
        if subject in seen_subjects:
            continue
        meta = ev.metadata or {}
        to_state = meta.get("to")
        if to_state != "on_hold":
            continue
        if ts > cutoff:
            continue
        key = _task_subject_key(subject)
        if key is None:
            continue
        project, _ = key
        stuck_minutes = max(1, int((now - ts).total_seconds() // 60))
        reason = meta.get("reason")
        routing = _classify_on_hold_reason(
            reason if isinstance(reason, str) else None,
        )
        seen_subjects.add(subject)
        findings.append(Finding(
            rule=RULE_TASK_ON_HOLD_STALE,
            tier=TIER_2,
            project=project,
            subject=subject,
            message=(
                f"Task {subject} has been at status=on_hold for "
                f"~{stuck_minutes} min — escalating to architect for "
                f"first-responder unstick."
            ),
            recommendation=(
                f"Architect: re-read the reviewer's rationale, fix the "
                f"issue, then `pm task queue {subject}`. Only escalate "
                f"to user via `pm notify` if the issue genuinely needs "
                f"human judgement."
            ),
            metadata={
                "on_hold_since": ev.ts,
                "stuck_minutes": stuck_minutes,
                "from": meta.get("from"),
                "reason": reason if isinstance(reason, str) else None,
                "routing": routing,
                "actor": ev.actor,
                "detected_via": "event",
            },
        ))
    return findings


def _latest_worker_heartbeat_time(
    events: Sequence[AuditEvent],
    *,
    subject: str,
    since: datetime,
) -> datetime | None:
    """Return the latest ``worker.heartbeat`` timestamp for ``subject``."""
    latest: datetime | None = None
    for ev in events:
        if ev.event != "worker.heartbeat":
            continue
        meta = ev.metadata or {}
        event_subjects = {
            ev.subject,
            str(meta.get("task_id") or ""),
            str(meta.get("subject") or ""),
        }
        if subject not in event_subjects:
            continue
        ts = _parse_iso(ev.ts)
        if ts is None or ts < since:
            continue
        if latest is None or ts > latest:
            latest = ts
    return latest


def _latest_task_context_time(
    task: Any, *, since: datetime,
) -> tuple[datetime | None, str]:
    """Return latest task-context timestamp after ``since``, if loaded."""
    latest: datetime | None = None
    latest_kind = ""
    for entry in getattr(task, "context", None) or []:
        ts = _coerce_datetime(getattr(entry, "timestamp", None))
        if ts is None or ts < since:
            continue
        if latest is None or ts > latest:
            latest = ts
            entry_type = getattr(entry, "entry_type", None) or "context"
            latest_kind = f"context:{entry_type}"
    return latest, latest_kind


def _detect_task_progress_stale(
    events: Sequence[AuditEvent],
    *,
    now: datetime,
    config: WatchdogConfig,
    open_tasks: Sequence[Any] | None = None,
) -> list[Finding]:
    """Rule 5c (#1444): ``in_progress`` tasks with no recent progress.

    The production path uses ``open_tasks`` so the detector keys off the
    authoritative current work state, then measures activity from the
    latest of entry into ``in_progress``, a future ``worker.heartbeat``
    audit row for the same task, or a loaded task-context entry.

    Audit-event fallback keeps ``scan_events`` useful for synthetic
    fixtures and future producers that emit ``worker.heartbeat``.
    """
    findings: list[Finding] = []
    seen_subjects: set[str] = set()
    cutoff = now - timedelta(seconds=config.progress_stale_seconds)

    if open_tasks:
        for task in open_tasks:
            status = getattr(task, "work_status", None)
            status_value = getattr(status, "value", status)
            if status_value != "in_progress":
                continue
            project = getattr(task, "project", "") or ""
            task_number = getattr(task, "task_number", None)
            if not project or task_number is None:
                continue
            subject = f"{project}/{task_number}"
            entry_ts, tr_meta = _state_entry_time(task, "in_progress")
            if entry_ts is None:
                continue

            last_activity = entry_ts
            last_activity_kind = "state_entry"
            heartbeat_ts = _latest_worker_heartbeat_time(
                events, subject=subject, since=entry_ts,
            )
            if heartbeat_ts is not None and heartbeat_ts > last_activity:
                last_activity = heartbeat_ts
                last_activity_kind = "worker.heartbeat"
            context_ts, context_kind = _latest_task_context_time(
                task, since=entry_ts,
            )
            if context_ts is not None and context_ts > last_activity:
                last_activity = context_ts
                last_activity_kind = context_kind

            seen_subjects.add(subject)
            if last_activity > cutoff:
                continue
            stuck_minutes = max(
                1, int((now - last_activity).total_seconds() // 60),
            )
            in_progress_minutes = max(
                1, int((now - entry_ts).total_seconds() // 60),
            )
            findings.append(Finding(
                rule=RULE_TASK_PROGRESS_STALE,
                tier=TIER_2,
                project=project,
                subject=subject,
                message=(
                    f"Task {subject} has been at status=in_progress for "
                    f"~{in_progress_minutes} min with no worker heartbeat "
                    f"or progress activity for ~{stuck_minutes} min."
                ),
                recommendation=(
                    f"Architect: inspect the worker for {subject}; check "
                    f"auth, sandbox, and worker logic, then reassign, "
                    f"resume, or cancel the task."
                ),
                metadata={
                    "in_progress_since": entry_ts.isoformat(),
                    "in_progress_minutes": in_progress_minutes,
                    "last_activity_at": last_activity.isoformat(),
                    "last_activity_kind": last_activity_kind,
                    "stuck_minutes": stuck_minutes,
                    "from": tr_meta.get("from"),
                    "actor": tr_meta.get("actor"),
                    "assignee": getattr(task, "assignee", None),
                    "current_node_id": getattr(task, "current_node_id", None),
                    "detected_via": "state",
                },
            ))

    latest_transition: dict[str, tuple[datetime, AuditEvent]] = {}
    for ev in events:
        if ev.event != EVENT_TASK_STATUS_CHANGED:
            continue
        ts = _parse_iso(ev.ts)
        if ts is None:
            continue
        prior = latest_transition.get(ev.subject)
        if prior is None or ts > prior[0]:
            latest_transition[ev.subject] = (ts, ev)

    for subject, (entry_ts, ev) in latest_transition.items():
        if subject in seen_subjects:
            continue
        meta = ev.metadata or {}
        if meta.get("to") != "in_progress":
            continue
        key = _task_subject_key(subject)
        if key is None:
            continue
        project, _ = key
        last_activity = entry_ts
        last_activity_kind = "task.status_changed"
        heartbeat_ts = _latest_worker_heartbeat_time(
            events, subject=subject, since=entry_ts,
        )
        if heartbeat_ts is not None and heartbeat_ts > last_activity:
            last_activity = heartbeat_ts
            last_activity_kind = "worker.heartbeat"
        if last_activity > cutoff:
            continue
        stuck_minutes = max(
            1, int((now - last_activity).total_seconds() // 60),
        )
        in_progress_minutes = max(
            1, int((now - entry_ts).total_seconds() // 60),
        )
        seen_subjects.add(subject)
        findings.append(Finding(
            rule=RULE_TASK_PROGRESS_STALE,
            tier=TIER_2,
            project=project,
            subject=subject,
            message=(
                f"Task {subject} has been at status=in_progress for "
                f"~{in_progress_minutes} min with no worker heartbeat "
                f"or progress activity for ~{stuck_minutes} min."
            ),
            recommendation=(
                f"Architect: inspect the worker for {subject}; check "
                f"auth, sandbox, and worker logic, then reassign, "
                f"resume, or cancel the task."
            ),
            metadata={
                "in_progress_since": ev.ts,
                "in_progress_minutes": in_progress_minutes,
                "last_activity_at": last_activity.isoformat(),
                "last_activity_kind": last_activity_kind,
                "stuck_minutes": stuck_minutes,
                "from": meta.get("from"),
                "actor": ev.actor,
                "detected_via": "event",
            },
        ))
    return findings


def _detect_role_session_missing(
    events: Sequence[AuditEvent],
    *,
    now: datetime,
    config: WatchdogConfig,
    open_tasks: Sequence[Any] | None = None,
    storage_window_names: Sequence[str] | None = None,
    project: str = "",
) -> list[Finding]:
    """Rule 6: in-flight task whose assigned role has no tmux session.

    Iterates ``open_tasks`` (in_progress / review with an assignee or
    role mapping). For each task whose role can be resolved, check
    whether ``<role>-<project>`` exists in the storage-closet window
    list. If not, fire.

    The window-name list is passed in (not queried here) so the pure
    detector can be unit-tested without spinning up tmux.
    """
    findings: list[Finding] = []
    if not open_tasks or storage_window_names is None:
        return findings
    window_set = {str(name).strip() for name in storage_window_names if name}

    for task in open_tasks:
        status = getattr(task, "work_status", None)
        # Accept either a WorkStatus enum or a raw string.
        status_value = getattr(status, "value", status)
        # #1546 — widened from ("in_progress", "review") to also cover
        # ``queued``. The pre-#1546 filter missed queued advisor tasks
        # whose role-<project> lane never spawned: the auto-claim sweep
        # cancels them after a few ticks and the advisor sweeper
        # re-creates them, producing the 52-cancellation cycle observed
        # in the coffeeboardnm session. Detecting at queued lets the
        # tier-1 self-heal spawn the lane *before* the auto-claim path
        # cancels the task, breaking the cycle at its source.
        if status_value not in ("in_progress", "review", "queued"):
            continue
        task_project = getattr(task, "project", "") or project
        task_number = getattr(task, "task_number", None)
        if not task_project or task_number is None:
            continue
        # Role resolution preference: explicit roles dict (advisor /
        # architect / reviewer / worker) → assignee fallback. We pick
        # the most specific role for the current state — review tasks
        # need a reviewer; in_progress / queued tasks need a worker
        # (or a role explicitly set on the task).
        #
        # ``advisor`` (#1546) — production advisor tasks land with
        # ``roles={"advisor": "advisor"}`` (see
        # ``plugins_builtin/advisor/handlers/advisor_tick.py``). The
        # canonical lane is ``advisor-<project>``. Pre-fix this
        # detector only walked worker/architect/reviewer, so a queued
        # advisor task whose advisor lane wasn't running silently
        # missed the auto-spawn — the regression #1546 was filed to
        # close.
        roles = getattr(task, "roles", {}) or {}
        candidate_roles: list[str] = []
        if status_value == "review":
            candidate_roles = ["reviewer", "architect", "worker", "advisor"]
        else:
            candidate_roles = ["worker", "architect", "reviewer", "advisor"]
        role_used: str | None = None
        for role in candidate_roles:
            if role in roles and roles[role]:
                role_used = role
                break
        if role_used is None:
            assignee = getattr(task, "assignee", None)
            if assignee:
                # Best-effort: use assignee as the role token. Most of
                # PollyPM's per-task workers run as ``worker-<project>``,
                # so this is the right default when no roles are set.
                role_used = "worker"
        if role_used is None:
            continue
        expected_window = f"{role_used}-{task_project}"
        if expected_window in window_set:
            continue
        findings.append(Finding(
            rule=RULE_ROLE_SESSION_MISSING,
            tier=TIER_1,
            project=task_project,
            subject=f"{task_project}/{task_number}",
            message=(
                f"Task {task_project}/{task_number} is at "
                f"status={status_value} but no '{expected_window}' "
                f"window exists in the storage-closet — the role agent "
                f"is not running."
            ),
            recommendation=(
                f"Spawn a `{role_used}` for {task_project} (e.g. "
                f"`pm chat {task_project} --role {role_used}`) or "
                f"reassign the task."
            ),
            metadata={
                "expected_window": expected_window,
                "role": role_used,
                "status": status_value,
            },
        ))
    return findings


def _detect_worker_session_dead_loop(
    events: Sequence[AuditEvent],
    *,
    now: datetime,
    config: WatchdogConfig,
) -> list[Finding]:
    """Rule 7: 3+ ``worker.session_reaped`` for the same task in a 10-min window.

    Each reaping is itself a sign the worker session crashed; three in
    a row means the underlying problem isn't the session, it's the
    task — and the architect needs to look at it instead of the reaper
    burning energy in a loop.
    """
    findings: list[Finding] = []
    cutoff = now - timedelta(seconds=config.dead_loop_window_seconds)

    # subject -> [(ts, ev), ...]
    by_subject: dict[str, list[tuple[datetime, AuditEvent]]] = {}
    for ev in events:
        if ev.event != EVENT_WORKER_SESSION_REAPED:
            continue
        ts = _parse_iso(ev.ts)
        if ts is None or ts < cutoff:
            continue
        if not ev.subject:
            continue
        by_subject.setdefault(ev.subject, []).append((ts, ev))

    for subject, hits in by_subject.items():
        if len(hits) < config.dead_loop_threshold:
            continue
        key = _task_subject_key(subject)
        if key is None:
            continue
        project, _ = key
        latest_ts, latest_ev = max(hits, key=lambda x: x[0])
        findings.append(Finding(
            rule=RULE_WORKER_SESSION_DEAD_LOOP,
            tier=TIER_2,
            project=project,
            subject=subject,
            message=(
                f"Worker session for {subject} was reaped "
                f"{len(hits)} times in the last "
                f"{config.dead_loop_window_seconds // 60} min — the "
                f"reaper is firing in a loop."
            ),
            recommendation=(
                f"Inspect the task and decide whether to cancel "
                f"(`pm task cancel {subject}`), reassign, or fix the "
                f"underlying spawn failure."
            ),
            metadata={
                "reap_count": len(hits),
                "latest_at": latest_ev.ts,
                "latest_reason": (latest_ev.metadata or {}).get("reason"),
            },
        ))
    return findings


def _detect_duplicate_advisor_tasks(
    *,
    open_tasks: Sequence[Any] | None,
) -> list[Finding]:
    """Rule (#1510): >1 non-terminal advisor-labeled task on the same project.

    The advisor.tick handler enqueues at most one ``advisor_review`` per
    project per cycle (see ``has_in_progress_advisor_task``). Two ticks
    racing on the throttle pre-#1510 produced clusters of 2–3 duplicates;
    even with the lock-based fix, a stale workspace can still carry the
    historical pile-up. This detector finds the wedge so the cadence
    handler can cancel every duplicate except the newest one.

    Pure detector — no I/O. The cadence handler does the cancel via the
    work-service API once the finding lands. ``metadata['duplicate_ids']``
    enumerates the task ids slated for cleanup so the action path
    doesn't have to re-walk ``open_tasks``.
    """
    if not open_tasks:
        return []

    by_project: dict[str, list[Any]] = {}
    for task in open_tasks:
        labels = list(getattr(task, "labels", []) or [])
        if "advisor" not in labels:
            continue
        status = getattr(task, "work_status", None)
        status_value = getattr(status, "value", status)
        if status_value not in ("draft", "queued", "in_progress", "review", "rework"):
            continue
        project = getattr(task, "project", "") or ""
        task_number = getattr(task, "task_number", None)
        if not project or task_number is None:
            continue
        by_project.setdefault(project, []).append(task)

    findings: list[Finding] = []
    for project, tasks in by_project.items():
        if len(tasks) <= 1:
            continue
        # Sort newest first by created_at, falling back to task_number
        # so a corrupted timestamp still produces a deterministic
        # "keep the highest task_number" outcome.
        def _sort_key(t: Any) -> tuple[Any, int]:
            created_at = getattr(t, "created_at", None)
            if created_at is None:
                # Coerce missing timestamps to epoch so they sort oldest.
                created_at = datetime.min.replace(tzinfo=timezone.utc)
            elif getattr(created_at, "tzinfo", None) is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            return (created_at, getattr(t, "task_number", 0) or 0)

        tasks_sorted = sorted(tasks, key=_sort_key, reverse=True)
        keep = tasks_sorted[0]
        duplicates = tasks_sorted[1:]
        keep_id = f"{project}/{getattr(keep, 'task_number', '?')}"
        duplicate_ids = [
            f"{project}/{getattr(t, 'task_number', '?')}" for t in duplicates
        ]
        findings.append(Finding(
            rule=RULE_DUPLICATE_ADVISOR_TASKS,
            tier=TIER_1,
            project=project,
            subject=keep_id,
            message=(
                f"Project {project} has {len(tasks)} non-terminal "
                f"advisor-labeled tasks; expected 1. Keeping the newest "
                f"({keep_id}) and cleaning up "
                f"{len(duplicates)} duplicate(s)."
            ),
            recommendation=(
                f"The cadence handler will cancel: "
                f"{', '.join(duplicate_ids)}. No operator action required."
            ),
            metadata={
                "keep_task_id": keep_id,
                "duplicate_ids": duplicate_ids,
                "total_count": len(tasks),
                "detected_via": "state",
            },
        ))
    return findings


# ---------------------------------------------------------------------------
# Plan-review missing detector (#1511)
# ---------------------------------------------------------------------------


def _detect_plan_review_missing(
    *,
    now: datetime,
    config: WatchdogConfig,
    done_plan_tasks: Sequence[Any] | None = None,
    plan_review_present: Any = None,
) -> list[Finding]:
    """Rule 9 (#1511): plan-shaped done task with no ``plan_review`` row.

    The architect's reflection node (#1399) only fires inside the
    ``plan_project`` flow. Plan-shaped tasks built on the ``standard``
    flow (or any other) reach ``done`` with no ``plan_review`` inbox
    card, so the cockpit's 2-line approval surface stays silent. This
    detector finds those stranded tasks and emits a finding so the
    cadence handler can backfill the missing card via the same emit
    path :mod:`pollypm.work.plan_review_emit` uses on the post-done
    hook (Part A of the #1511 fix).

    Inputs are passed in by the scan caller so this function stays
    pure / unit-testable:

    * ``done_plan_tasks`` — already filtered to ``work_status=done`` +
      plan-shaped labels + non-plan_project flow + ``created_at`` within
      ``plan_review_lookback_seconds``. The cadence handler builds this
      list via ``svc.list_tasks(work_status='done', project=...)`` +
      :func:`pollypm.work.plan_review_emit.task_is_eligible_for_backstop`.
    * ``plan_review_present`` — a callable taking ``(project,
      plan_task_id)`` and returning True if a plan_review message
      already exists. Tests pass a ``set``-backed lambda; production
      passes :func:`pollypm.work.plan_review_emit.already_has_plan_review_message`.

    Both inputs default to None — the detector then no-ops, which is
    the right default when the caller can't enumerate done tasks /
    probe the messages table.
    """
    findings: list[Finding] = []
    if not done_plan_tasks:
        return findings
    if plan_review_present is None:
        return findings
    cutoff = now - timedelta(seconds=config.plan_review_lookback_seconds)
    for task in done_plan_tasks:
        project = getattr(task, "project", "") or ""
        task_number = getattr(task, "task_number", None)
        if not project or task_number is None:
            continue
        plan_task_id = f"{project}/{task_number}"
        created_at = getattr(task, "created_at", None)
        if isinstance(created_at, datetime):
            ts = created_at if created_at.tzinfo is not None else created_at.replace(
                tzinfo=timezone.utc,
            )
            if ts < cutoff:
                continue
        try:
            already_present = bool(plan_review_present(project, plan_task_id))
        except Exception:  # noqa: BLE001
            logger.debug(
                "plan_review_missing: probe failed for %s",
                plan_task_id, exc_info=True,
            )
            continue
        if already_present:
            continue
        labels = list(getattr(task, "labels", None) or [])
        flow_id = getattr(task, "flow_template_id", "") or ""
        findings.append(Finding(
            rule=RULE_PLAN_REVIEW_MISSING,
            tier=TIER_1,
            project=project,
            subject=plan_task_id,
            message=(
                f"Plan-shaped task {plan_task_id} (flow={flow_id or '?'}) "
                f"reached done with no plan_review inbox card."
            ),
            recommendation=(
                f"Backfill via plan_review_emit.emit_plan_review_for_task "
                f"so the cockpit approval surface renders for {plan_task_id}."
            ),
            metadata={
                "plan_task_id": plan_task_id,
                "flow_template_id": flow_id,
                "labels": labels,
                "created_at": (
                    created_at.isoformat() if isinstance(created_at, datetime)
                    else None
                ),
                "detected_via": "state",
            },
        ))
    return findings


# ---------------------------------------------------------------------------
# Legacy-DB shadow detector (#1519)
# ---------------------------------------------------------------------------


def _detect_legacy_db_shadow(
    *,
    legacy_db_shadows: Sequence[Any] | None = None,
) -> list[Finding]:
    """Rule (#1519): a project has rows in BOTH canonical and legacy DBs.

    Pre-#1004 the per-project ``<project>/.pollypm/state.db`` was the
    primary task store; #1004 collapsed the resolver to the workspace
    root. ``migrate_legacy_per_project_dbs`` exists to drain leftover
    files but only runs if invoked explicitly. The savethenovel /
    coffeeboardnm post-mortem (#1519) showed the dashboard pipeline
    surfacing 6 queued (1 canonical + 5 stale legacy advisor_review
    rows) because the read path unioned both DBs. The read-path fix
    (Part A) prefers canonical, but the divergence itself is still a
    bug — operator data is split across two files and any tool that
    reads only one is wrong. This detector finds the wedge so the
    cadence handler can drain the legacy DB.

    Pure detector — no I/O. The cadence handler probes the filesystem
    + sqlite for each project and passes a list of ``LegacyDbShadow``-
    shaped objects (``project_key``, ``project_path``, ``canonical_db``,
    ``legacy_db``, ``legacy_row_count``, ``canonical_row_count``).
    The detector emits one finding per shadowed project. The action
    path (``migrate_legacy_per_project_dbs``) is idempotent: rows
    already in canonical are skipped by ``(project, task_number)``,
    the legacy file is archived to ``state.db.legacy-1004`` only after
    every row was either copied or matched. ``None`` disables the rule.
    """
    findings: list[Finding] = []
    if not legacy_db_shadows:
        return findings
    for shadow in legacy_db_shadows:
        project_key = getattr(shadow, "project_key", "") or ""
        legacy_db = getattr(shadow, "legacy_db", None)
        canonical_db = getattr(shadow, "canonical_db", None)
        legacy_rows = int(getattr(shadow, "legacy_row_count", 0) or 0)
        canonical_rows = int(
            getattr(shadow, "canonical_row_count", 0) or 0,
        )
        if not project_key or legacy_db is None or canonical_db is None:
            continue
        if legacy_rows <= 0 or canonical_rows <= 0:
            # A pure-canonical or pure-legacy project isn't a shadow —
            # the read-path fallback already routes it correctly.
            continue
        legacy_path_str = str(legacy_db)
        canonical_path_str = str(canonical_db)
        findings.append(Finding(
            rule=RULE_LEGACY_DB_SHADOW,
            tier=TIER_1,
            project=project_key,
            subject=legacy_path_str,
            message=(
                f"Project {project_key} has rows in BOTH the canonical "
                f"workspace DB ({canonical_path_str}: {canonical_rows} "
                f"row(s)) AND its legacy per-project DB "
                f"({legacy_path_str}: {legacy_rows} row(s)). The "
                f"dashboard's pre-#1519 union read produced inflated "
                f"counts; auto-migration drains the legacy file."
            ),
            recommendation=(
                f"Cadence handler will run "
                f"``migrate_legacy_per_project_dbs`` for {project_key} "
                f"— idempotent, archives legacy file to "
                f"``state.db.legacy-1004`` after a successful drain."
            ),
            metadata={
                "project_key": project_key,
                "canonical_db": canonical_path_str,
                "legacy_db": legacy_path_str,
                "canonical_row_count": canonical_rows,
                "legacy_row_count": legacy_rows,
                "detected_via": "state",
            },
        ))
    return findings


# ---------------------------------------------------------------------------
# Plan_missing alert churn detector (#1524)
# ---------------------------------------------------------------------------


# Project-key extractor for synthetic ``plan_gate-<project>`` scopes.
# Pinned to a regex so a future scope-rename catches itself in tests.
_PLAN_GATE_SCOPE_RE = re.compile(r"^plan_gate-(?P<project>.+)$")


def _detect_plan_missing_alert_churn(
    *,
    now: datetime,
    config: WatchdogConfig,
    plan_missing_clears: Sequence[Any] | None = None,
) -> list[Finding]:
    """Rule (#1524): a project's ``plan_missing`` alert is being cleared
    + re-emitted repeatedly inside a short window — the banner-flash
    regression that Part A of #1524 repairs.

    Pre-#1524 the task-assignment sweep handler had a fragile coupling
    between the auto-claim loop's side-effect-populated
    ``plan_missing_projects`` set and the per-project DB clear path.
    After the per-project legacy DB was drained (post-#1519), a
    project's ``plan_missing`` alert was being cleared on ticks where
    the auto-claim path didn't traverse it and re-emitted on the next
    tick from the workspace-root pass — producing alternating
    "Waiting on you" / "Queued" banner state and a fresh alert row
    every other sweep tick. Part A (the deterministic precompute in
    ``_task_assignment_sweep_body``) is the repair; this detector is
    the canary that catches the regression returning.

    Pure detector — no I/O. The cadence handler queries the
    ``messages`` table for ``alert.cleared`` events with
    ``sender='plan_missing'`` in the configured window and passes the
    clear records in here (one entry per clear event, exposing
    ``scope`` and ``cleared_at``). The detector groups by scope,
    counts clears per project, and emits a finding for each scope at
    or above ``plan_missing_churn_threshold``.

    Each clear record must expose:

    * ``scope`` — the alert ``scope`` (synthetic
      ``plan_gate-<project>`` for plan_missing alerts).
    * ``cleared_at`` — a tz-aware ``datetime`` (or ISO-8601 string)
      for when the clear landed.

    ``plan_missing_clears=None`` disables the rule (the right default
    when the cadence caller can't query the messages table).
    """
    findings: list[Finding] = []
    if not plan_missing_clears:
        return findings
    cutoff = now - timedelta(seconds=config.plan_missing_churn_window_seconds)

    by_project: dict[str, list[datetime]] = {}
    for record in plan_missing_clears:
        scope_value = getattr(record, "scope", None)
        if not scope_value:
            continue
        match = _PLAN_GATE_SCOPE_RE.match(str(scope_value))
        if match is None:
            # Non-plan-gate scope — wrong rule for this row.
            continue
        project_key = match.group("project")
        if not project_key:
            continue
        cleared_at_raw = getattr(record, "cleared_at", None)
        cleared_at = _coerce_datetime(cleared_at_raw)
        if cleared_at is None:
            continue
        if cleared_at < cutoff:
            continue
        by_project.setdefault(project_key, []).append(cleared_at)

    for project_key, clears in by_project.items():
        if len(clears) < config.plan_missing_churn_threshold:
            continue
        clears.sort()
        latest_iso = clears[-1].isoformat()
        first_iso = clears[0].isoformat()
        scope_str = f"plan_gate-{project_key}"
        findings.append(Finding(
            rule=RULE_PLAN_MISSING_ALERT_CHURN,
            tier=TIER_TERMINAL,
            project=project_key,
            subject=scope_str,
            message=(
                f"Project {project_key} had {len(clears)} ``plan_missing`` "
                f"alert clear events between {first_iso} and {latest_iso} "
                f"(window: "
                f"{config.plan_missing_churn_window_seconds // 60} min). "
                f"The sweep handler is bouncing the alert open/closed each "
                f"tick — the #1524 banner-flash regression has returned."
            ),
            recommendation=(
                f"Inspect the task-assignment sweep handler "
                f"(``_task_assignment_sweep_body`` + "
                f"``_precompute_plan_missing_projects``). The pre-compute "
                f"step in Part A of #1524 should keep "
                f"plan_gate-{project_key} stable across consecutive ticks; "
                f"a regression in that path almost always traces to the "
                f"per-project DB clear branch firing for a project the "
                f"precompute should have flagged."
            ),
            metadata={
                "project_key": project_key,
                "clear_count": len(clears),
                "window_seconds": config.plan_missing_churn_window_seconds,
                "first_clear_at": first_iso,
                "latest_clear_at": latest_iso,
                "detected_via": "state",
            },
        ))
    return findings


# ---------------------------------------------------------------------------
# Rejection-loop detector (#1546)
# ---------------------------------------------------------------------------


# Tokens we strip when comparing reject-reason "stems" so that small
# wording variations between attempts don't defeat the cluster check.
# The point isn't lexical equality — it's "do these reasons share a
# root-cause family". A short stop-list is enough because the
# cluster check also looks at the failing node and node-specific
# error tokens (extracted via ``_REJECT_TOKEN_RE``).
_REJECT_REASON_STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "and", "or", "but", "so", "is", "was", "were",
    "be", "been", "to", "of", "in", "on", "at", "by", "for", "with",
    "without", "from", "as", "this", "that", "these", "those", "it",
    "its", "i", "we", "you", "they", "task", "rejected", "reject",
    "rejection", "reason", "reasons", "node", "review", "code",
    "test", "tests", "failed", "failing", "fails", "error", "errors",
    "issue", "issues", "fix", "please", "v1", "v2", "v3", "v4", "v5",
})

_REJECT_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]+")


def _reason_tokens(reason: str | None) -> set[str]:
    """Extract a stem-set from a reject reason for cluster comparison.

    Lowercases, splits on non-token characters, drops short tokens
    (<3 chars) and the stop-list. Returns the deduplicated set so a
    later overlap check is symmetric. We keep the symbol-bearing
    tokens (e.g. ``better-sqlite3``, ``Node25``, ``ENOENT``) because
    those are the high-signal terms when reject reasons cluster
    around a dependency / runtime / error code.
    """
    if not reason:
        return set()
    out: set[str] = set()
    for raw in _REJECT_TOKEN_RE.findall(reason):
        token = raw.lower()
        if len(token) < 3:
            continue
        if token in _REJECT_REASON_STOPWORDS:
            continue
        out.add(token)
    return out


def _reasons_cluster(
    reasons: Sequence[str | None],
    *,
    nodes: Sequence[str | None],
    min_overlap: int = 1,
) -> tuple[bool, list[str]]:
    """Return ``(clustered, shared_tokens)`` for a set of reject reasons.

    The cluster predicate is:

    * Every reject must be at the same review node — handled by the
      caller (we only see reasons that already share a node).
    * Across the reasons, at least ``min_overlap`` tokens appear in
      every reason's token set. The default is 1 because the stop-list
      already filters out generic words (``test``, ``error``, …); a
      single shared symbol-bearing token like ``better-sqlite3`` /
      ``ENOENT`` / ``node25`` is the "shared error-family" signal the
      spec calls out. Tests can pass ``min_overlap=2`` for a stricter
      bar.

    Returns ``(False, [])`` if reasons / nodes lists don't carry
    enough signal (empty / all None) to call a cluster.
    """
    if not reasons:
        return False, []
    token_sets = [_reason_tokens(r) for r in reasons]
    # Drop empty token sets — a missing reason is no signal either way.
    nonempty = [s for s in token_sets if s]
    if len(nonempty) < 2:
        return False, []
    shared = set.intersection(*nonempty)
    if len(shared) < min_overlap:
        return False, []
    # Sort for deterministic forensic output.
    return True, sorted(shared)


def _detect_rejection_loop(
    *,
    now: datetime,
    config: WatchdogConfig,
    open_tasks: Sequence[Any] | None = None,
) -> list[Finding]:
    """Rule (#1546): K=3+ consecutive rejections at the same review node.

    Walks ``open_tasks`` (which the cadence handler hydrates with
    executions). For each task, looks at its ``executions`` list, picks
    out reviewer rows where ``decision == REJECTED`` (or whose
    ``decision_reason`` looks rejection-shaped on services that don't
    populate the enum), filters to the configured window, and groups
    by ``node_id``. A cluster of K+ rejects in the window at the same
    node whose reject reasons share a token-stem of size >= 2 fires a
    tier-1 finding routed to tier-2 dispatch.

    The detector is deliberately strict on cluster shape: it counts
    exactly K (or more) rejections in window at the *same* node. A
    task that bounced reject → fix → reject → fix at different nodes
    is a separate worker problem (a dependency-resolution loop, not a
    structural one) and gets caught by ``task_progress_stale`` /
    ``worker_session_dead_loop`` instead.

    Pure detector — no I/O. Returns an empty list when ``open_tasks``
    is missing (the right default for synthetic-fixture tests that
    don't hydrate executions).
    """
    findings: list[Finding] = []
    if not open_tasks:
        return findings
    cutoff = now - timedelta(seconds=config.rejection_loop_window_seconds)
    threshold = max(1, int(config.rejection_loop_threshold))

    for task in open_tasks:
        project = getattr(task, "project", "") or ""
        task_number = getattr(task, "task_number", None)
        if not project or task_number is None:
            continue
        executions = list(getattr(task, "executions", None) or [])
        if not executions:
            continue
        # Group rows by node_id and walk newest→oldest; the detector
        # fires only when the most-recent K rows at the same node are
        # *consecutively* rejections. Any non-rejection row (approval,
        # progress, status change) at that node resets the run — a
        # task that bounced reject → approve → reject is not stuck in
        # the structural-loop shape this rule flags.
        #
        # We accept either the ``Decision.REJECTED`` enum value OR a
        # string-valued decision column — :class:`SQLiteWorkService`
        # materialises Decision as the enum, but legacy / synthetic
        # test fixtures may carry the raw string.
        rows_by_node: dict[str, list[Any]] = {}
        for ex in executions:
            completed = getattr(ex, "completed_at", None) or getattr(
                ex, "started_at", None,
            )
            completed_dt = _coerce_datetime(completed)
            if completed_dt is None:
                continue
            node_id = getattr(ex, "node_id", "") or ""
            if not node_id:
                continue
            rows_by_node.setdefault(node_id, []).append(ex)

        rejects_by_node: dict[str, list[Any]] = {}
        for node_id, node_rows in rows_by_node.items():
            # Sort newest-first so we can read the consecutive run
            # off the head of the list.
            node_rows.sort(
                key=lambda ex: _coerce_datetime(
                    getattr(ex, "completed_at", None)
                    or getattr(ex, "started_at", None),
                ) or now,
                reverse=True,
            )
            consecutive: list[Any] = []
            for ex in node_rows:
                decision = getattr(ex, "decision", None)
                decision_value = getattr(decision, "value", decision)
                decision_norm = (
                    decision_value.lower()
                    if isinstance(decision_value, str)
                    else ""
                )
                if decision_norm != "rejected":
                    # Run broken — anything other than a rejection at
                    # the same node resets the consecutive counter.
                    break
                completed_dt = _coerce_datetime(
                    getattr(ex, "completed_at", None)
                    or getattr(ex, "started_at", None),
                )
                if completed_dt is None or completed_dt < cutoff:
                    # The run goes back too far; older rejections
                    # don't count toward the K-in-a-row contract.
                    break
                consecutive.append(ex)
            if consecutive:
                rejects_by_node[node_id] = consecutive

        for node_id, rows in rejects_by_node.items():
            if len(rows) < threshold:
                continue
            # ``rows`` is already newest-first by construction.
            recent = rows[:threshold]
            reasons = [
                (getattr(ex, "decision_reason", None) or "").strip()
                for ex in recent
            ]
            nodes = [getattr(ex, "node_id", "") for ex in recent]
            clustered, shared_tokens = _reasons_cluster(
                reasons, nodes=nodes, min_overlap=1,
            )
            if not clustered:
                continue
            subject = f"{project}/{task_number}"
            attempt_lines = [
                {
                    "node": getattr(ex, "node_id", "") or "",
                    "completed_at": (
                        _coerce_datetime(
                            getattr(ex, "completed_at", None)
                            or getattr(ex, "started_at", None),
                        )
                        or now
                    ).isoformat(),
                    "reason": reason or "",
                }
                for ex, reason in zip(recent, reasons)
            ]
            findings.append(Finding(
                rule=RULE_REJECTION_LOOP,
                # The detector itself is cheap and pure (the issue
                # frames it as "tier-1 detector"), but the dispatch
                # tier — what downstream consumers branch on — is
                # tier-2. The finding carries tier=TIER_2 so the
                # cadence handler routes it to the architect leg of
                # the cascade (structured evidence, no auto-cancel).
                tier=TIER_2,
                project=project,
                subject=subject,
                message=(
                    f"Task {subject} has {len(rows)} rejections at node "
                    f"'{node_id}' inside the last "
                    f"{config.rejection_loop_window_seconds // 60} min — "
                    f"the worker is in a structural loop."
                ),
                recommendation=(
                    f"Hand structured evidence to the project PM for "
                    f"{subject}; do NOT auto-cancel."
                ),
                metadata={
                    "node_id": node_id,
                    "reject_count": len(rows),
                    "window_seconds": config.rejection_loop_window_seconds,
                    "shared_tokens": shared_tokens,
                    "detected_via": "state",
                },
                evidence={
                    "node_id": node_id,
                    "reject_count": len(rows),
                    "attempts": attempt_lines,
                    "shared_tokens": shared_tokens,
                    "window_seconds": config.rejection_loop_window_seconds,
                },
            ))
    return findings


# ---------------------------------------------------------------------------
# Missing canonical state.db detector (#1546 tier-1)
# ---------------------------------------------------------------------------


def _detect_state_db_missing(
    *,
    state_db_probes: Sequence[Any] | None = None,
) -> list[Finding]:
    """Rule (#1546 tier-1): canonical ``.pollypm/state.db`` is missing.

    Each probe entry must expose:

    * ``project_key`` — the project key.
    * ``project_path`` — the project root (Path or str).
    * ``canonical_present`` — True iff ``.pollypm/state.db`` exists.
    * ``legacy_archives_present`` — True iff one or more
      ``.pollypm/state.db.legacy-*`` files exist (signal that this is a
      project that *had* a canonical DB but no longer has one).

    Fires only when canonical is absent. The legacy-archives signal is
    advisory — it tells the architect this is the "post-archival"
    failure mode, not a brand-new project that simply hasn't been
    initialised. Heal action lives in the cadence handler and calls
    ``enable_tracked_project`` to re-run the scaffold.

    Pure detector. ``None`` disables the rule (the right default when
    the cadence caller didn't probe the filesystem).
    """
    findings: list[Finding] = []
    if not state_db_probes:
        return findings
    for probe in state_db_probes:
        project_key = getattr(probe, "project_key", "") or ""
        project_path = getattr(probe, "project_path", None)
        canonical_present = bool(
            getattr(probe, "canonical_present", False),
        )
        legacy_archives_present = bool(
            getattr(probe, "legacy_archives_present", False),
        )
        canonical_db_path_raw = getattr(probe, "canonical_db_path", None)
        canonical_db_path_str = (
            str(canonical_db_path_raw) if canonical_db_path_raw is not None else ""
        )
        if not project_key or canonical_present:
            continue
        path_str = str(project_path) if project_path is not None else ""
        findings.append(Finding(
            rule=RULE_STATE_DB_MISSING,
            tier=TIER_1,
            project=project_key,
            subject=project_key,
            message=(
                f"Workspace canonical state.db is missing"
                + (f" at {canonical_db_path_str}" if canonical_db_path_str else "")
                + (
                    " — only legacy archives remain (state.db.legacy-*)."
                    if legacy_archives_present else
                    "; the workspace tracker scaffold may have been "
                    "removed."
                )
            ),
            recommendation=(
                f"Re-create the workspace state.db by opening it via "
                f"``create_work_service`` (the schema replay rebuilds "
                f"the tables); the heartbeat self-heal does this "
                f"automatically."
            ),
            metadata={
                "project_key": project_key,
                "project_path": path_str,
                "canonical_db_path": canonical_db_path_str,
                "legacy_archives_present": legacy_archives_present,
                "detected_via": "state",
            },
            evidence={
                "project_key": project_key,
                "project_path": path_str,
                "canonical_db_path": canonical_db_path_str,
                "canonical_present": canonical_present,
                "legacy_archives_present": legacy_archives_present,
            },
        ))
    return findings


# ---------------------------------------------------------------------------
# Generic safety-net probe framework (#1546)
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ProbeContext:
    """Inputs a project-level safety-net probe sees.

    Probes are pure functions of this context — they receive the
    project key, the open-task snapshot, recent audit events, and the
    wall clock, and emit zero or more :class:`Finding` objects.
    Filesystem / DB lookups are not allowed: anything a probe needs
    must be hydrated by the cadence handler and exposed here, so the
    probe stays unit-testable without a workspace.
    """

    project_key: str
    now: datetime
    config: WatchdogConfig
    open_tasks: Sequence[Any]
    events: Sequence[AuditEvent]


# Probe registry — public so future PRs can register new probes
# without touching ``scan_events``. Each entry is a callable
# ``(ProbeContext) -> list[Finding]``.
_SAFETY_NET_PROBES: list[Any] = []


def register_safety_net_probe(probe: Any) -> Any:
    """Register a project-level safety-net probe.

    Returns the probe unchanged so it can be used as a decorator.
    Probes registered here are run once per project per heartbeat
    tick, in registration order. The probe is responsible for its own
    threshold / window logic; the framework provides no per-probe
    throttling because the dispatcher handles dedup at the
    finding → dispatch boundary.
    """
    _SAFETY_NET_PROBES.append(probe)
    return probe


def _run_safety_net_probes(ctx: ProbeContext) -> list[Finding]:
    """Run every registered probe against ``ctx`` and collect findings."""
    out: list[Finding] = []
    for probe in _SAFETY_NET_PROBES:
        try:
            out.extend(probe(ctx) or [])
        except Exception:  # noqa: BLE001
            logger.debug(
                "safety-net probe %s raised; skipping",
                getattr(probe, "__name__", repr(probe)),
                exc_info=True,
            )
    return out


# Activity events that count as "queue motion" — at least one of these
# inside the threshold window means the queue isn't actually wedged,
# even if there are queued tasks.
_QUEUE_MOTION_EVENTS: frozenset[str] = frozenset({
    EVENT_TASK_STATUS_CHANGED,
    EVENT_TASK_CREATED,
    "task.claimed",
    "execution.advanced",
    "execution.completed",
    "worker.heartbeat",
})


def _queue_without_motion_probe(ctx: ProbeContext) -> list[Finding]:
    """Safety-net probe: queued tasks but no recent activity events.

    Detects "the queue is full but nothing's moving" — fires a tier-2
    finding (routed to operator dispatch in the cadence handler today,
    because a stalled queue typically outlives a tier-2 PM dispatch).
    Evidence carries the affected task IDs and the most-recent activity
    timestamp so the prompt builder can hand structured signal upward.
    """
    cutoff = ctx.now - timedelta(
        seconds=ctx.config.queue_motion_threshold_seconds,
    )
    queued_subjects: list[str] = []
    queued_oldest: dict[str, str] = {}
    for task in ctx.open_tasks or ():
        status = getattr(task, "work_status", None)
        status_value = getattr(status, "value", status)
        if status_value != "queued":
            continue
        project = getattr(task, "project", "") or ""
        task_number = getattr(task, "task_number", None)
        if not project or task_number is None:
            continue
        if project != ctx.project_key:
            continue
        subject = f"{project}/{task_number}"
        queued_subjects.append(subject)
        updated_at = getattr(task, "updated_at", None) or getattr(
            task, "created_at", None,
        )
        if isinstance(updated_at, datetime):
            queued_oldest[subject] = (
                updated_at if updated_at.tzinfo is not None
                else updated_at.replace(tzinfo=timezone.utc)
            ).isoformat()
    if not queued_subjects:
        return []

    last_activity_ts: datetime | None = None
    last_activity_event: str = ""
    for ev in ctx.events or ():
        if ev.event not in _QUEUE_MOTION_EVENTS:
            continue
        if ev.project and ev.project != ctx.project_key:
            continue
        ts = _parse_iso(ev.ts)
        if ts is None:
            continue
        if last_activity_ts is None or ts > last_activity_ts:
            last_activity_ts = ts
            last_activity_event = ev.event
    if last_activity_ts is not None and last_activity_ts >= cutoff:
        return []

    last_iso = last_activity_ts.isoformat() if last_activity_ts else None
    minutes_silent = int(
        (ctx.now - last_activity_ts).total_seconds() // 60
    ) if last_activity_ts else None
    return [Finding(
        rule=RULE_QUEUE_WITHOUT_MOTION,
        # #1546 — operator-level concern: a wedged queue is a
        # cross-cutting failure that has consistently outlived
        # tier-2 architect dispatches in the field. Routing it
        # straight to the operator (tier-3) matches the dispatch
        # rule registry and makes the contract honest.
        tier=TIER_3,
        project=ctx.project_key,
        subject=ctx.project_key,
        message=(
            f"Project {ctx.project_key} has {len(queued_subjects)} "
            f"queued task(s) but no claim / execution / status-change "
            f"activity for "
            + (f"~{minutes_silent} min." if minutes_silent is not None
               else "the entire scan window.")
        ),
        recommendation=(
            f"Hand structured evidence to the operator for "
            f"{ctx.project_key}; the queue is wedged."
        ),
        metadata={
            "queued_count": len(queued_subjects),
            "last_activity_at": last_iso,
            "last_activity_event": last_activity_event,
            "threshold_seconds": ctx.config.queue_motion_threshold_seconds,
            "detected_via": "probe",
        },
        evidence={
            "queued_subjects": list(queued_subjects),
            "queued_last_updated": dict(queued_oldest),
            "last_activity_at": last_iso,
            "last_activity_event": last_activity_event,
            "threshold_seconds": ctx.config.queue_motion_threshold_seconds,
        },
    )]


# Register the one shipped probe on import.
register_safety_net_probe(_queue_without_motion_probe)


# ---------------------------------------------------------------------------
# Public API — scan_events / scan_project
# ---------------------------------------------------------------------------


def scan_events(
    events: Iterable[AuditEvent],
    *,
    now: datetime,
    config: WatchdogConfig | None = None,
    open_tasks: Sequence[Any] | None = None,
    storage_window_names: Sequence[str] | None = None,
    project: str = "",
    done_plan_tasks: Sequence[Any] | None = None,
    plan_review_present: Any = None,
    legacy_db_shadows: Sequence[Any] | None = None,
    plan_missing_clears: Sequence[Any] | None = None,
    state_db_probes: Sequence[Any] | None = None,
    run_safety_net_probes: bool = True,
) -> list[Finding]:
    """Run every detection rule against ``events`` and return findings.

    Pure function — no I/O. Tests pass a synthetic event list directly.
    The events are iterated multiple times so an iterable is materialised
    into a list up front.

    Args:
        events: audit events to scan. Order need not be sorted —
            individual detectors compare timestamps.
        now: tz-aware "current" wall clock — typically
            ``datetime.now(timezone.utc)`` in production, a fixed
            instant in tests.
        config: tunable thresholds. ``None`` uses :class:`WatchdogConfig`
            defaults.
        open_tasks: live in-flight tasks for the ``role_session_missing``
            rule — typically the result of
            ``WorkService.list_nonterminal_tasks(project=...)``. ``None``
            disables the rule (e.g. unit tests that only feed events).
        storage_window_names: window names visible in the storage-closet
            tmux session. Passed in so ``role_session_missing`` stays a
            pure function.
        project: project key — only used by ``role_session_missing``
            when the open_task rows don't carry it.
        done_plan_tasks: completed plan-shaped tasks for the
            ``plan_review_missing`` rule (#1511). Passed in by the
            cadence handler so the detector stays pure. ``None`` disables
            the rule (the right default for unit tests).
        plan_review_present: callable ``(project, plan_task_id) -> bool``
            that returns True iff a ``plan_review`` message already exists
            in the messages table for that task. ``None`` disables the
            ``plan_review_missing`` rule.
        legacy_db_shadows: per-project shadow descriptors for the
            ``legacy_db_shadow`` rule (#1519). Each entry must expose
            ``project_key``, ``project_path``, ``canonical_db``,
            ``legacy_db``, ``canonical_row_count``, ``legacy_row_count``.
            The cadence handler builds this list by probing the
            workspace + per-project state.db files for each known
            project. ``None`` disables the rule.
    """
    if config is None:
        config = WatchdogConfig()
    if now.tzinfo is None:
        raise ValueError("scan_events requires a timezone-aware ``now``")
    materialised = list(events)
    findings: list[Finding] = []
    findings.extend(_detect_orphan_markers(
        materialised, now=now, config=config,
    ))
    findings.extend(_detect_marker_leaks(
        materialised, now=now, config=config,
    ))
    findings.extend(_detect_stuck_drafts(
        materialised, now=now, config=config, open_tasks=open_tasks,
    ))
    findings.extend(_detect_cancellation_no_promotion(
        materialised, now=now, config=config,
    ))
    findings.extend(_detect_task_review_stale(
        materialised, now=now, config=config, open_tasks=open_tasks,
    ))
    findings.extend(_detect_task_on_hold_stale(
        materialised, now=now, config=config, open_tasks=open_tasks,
    ))
    findings.extend(_detect_task_progress_stale(
        materialised, now=now, config=config, open_tasks=open_tasks,
    ))
    findings.extend(_detect_role_session_missing(
        materialised,
        now=now,
        config=config,
        open_tasks=open_tasks,
        storage_window_names=storage_window_names,
        project=project,
    ))
    findings.extend(_detect_worker_session_dead_loop(
        materialised, now=now, config=config,
    ))
    # #1510 — duplicate advisor tasks. State-only detector; no audit
    # events required because the wedge is observable from current
    # ``open_tasks`` alone (it's a write-time invariant violation, not
    # a temporal pattern).
    findings.extend(_detect_duplicate_advisor_tasks(open_tasks=open_tasks))
    findings.extend(_detect_plan_review_missing(
        now=now,
        config=config,
        done_plan_tasks=done_plan_tasks,
        plan_review_present=plan_review_present,
    ))
    # #1519 — legacy DB shadows the canonical workspace DB. State-only
    # detector; the cadence handler probes the filesystem + sqlite for
    # the row counts and passes them in so this function stays pure.
    findings.extend(_detect_legacy_db_shadow(
        legacy_db_shadows=legacy_db_shadows,
    ))
    # #1524 — plan_missing alert churn. State-only detector; the cadence
    # handler queries the messages table for ``alert.cleared`` events on
    # ``plan_gate-<project>`` scopes and passes one record per clear in
    # so this function stays pure.
    findings.extend(_detect_plan_missing_alert_churn(
        now=now,
        config=config,
        plan_missing_clears=plan_missing_clears,
    ))
    # #1546 — rejection-loop detector. Walks ``open_tasks`` executions
    # for clusters of rejections at the same review node within the
    # configured window. Fires tier-1 → tier-2 dispatch with structured
    # evidence; the action path does NOT auto-cancel.
    findings.extend(_detect_rejection_loop(
        now=now, config=config, open_tasks=open_tasks,
    ))
    # #1546 — state-db-missing detector. Pure detector that consumes
    # ``state_db_probes`` (one per project) and fires when the canonical
    # ``.pollypm/state.db`` is absent. Tier-1 self-heal lives in the
    # cadence handler.
    findings.extend(_detect_state_db_missing(
        state_db_probes=state_db_probes,
    ))
    # #1546 — generic safety-net probes. Each registered probe is run
    # once per project per scan; the framework swallows probe-side
    # exceptions so a buggy probe can't break the cadence. Probes are
    # opt-in via ``run_safety_net_probes`` so synthetic-fixture tests
    # that only exercise specific detectors aren't perturbed by a
    # generic probe firing.
    if run_safety_net_probes and project:
        ctx = ProbeContext(
            project_key=project,
            now=now,
            config=config,
            open_tasks=tuple(open_tasks or ()),
            events=materialised,
        )
        findings.extend(_run_safety_net_probes(ctx))
    return findings


def scan_project(
    project: str,
    *,
    project_path: Path | str | None = None,
    now: datetime | None = None,
    config: WatchdogConfig | None = None,
    open_tasks: Sequence[Any] | None = None,
    storage_window_names: Sequence[str] | None = None,
    done_plan_tasks: Sequence[Any] | None = None,
    plan_review_present: Any = None,
    legacy_db_shadows: Sequence[Any] | None = None,
    plan_missing_clears: Sequence[Any] | None = None,
    state_db_probes: Sequence[Any] | None = None,
    run_safety_net_probes: bool = True,
) -> list[Finding]:
    """Read audit events for ``project`` and scan them.

    Convenience wrapper used by the cadence handler. Pulls events
    from the per-project log when ``project_path`` exists, else
    from the central tail (mirrors :func:`pollypm.audit.read_events`
    source-preference rules).

    ``open_tasks`` and ``storage_window_names`` are pass-through inputs
    for the auto-unstick rules (#1414); ``done_plan_tasks`` +
    ``plan_review_present`` are pass-through for the plan_review_missing
    rule (#1511); ``legacy_db_shadows`` is pass-through for the
    legacy_db_shadow rule (#1519); ``plan_missing_clears`` is
    pass-through for the plan_missing_alert_churn rule (#1524). When
    omitted those rules become no-ops, which is the right default for
    callers that only have audit events on hand.
    """
    from pollypm.audit.log import read_events

    if config is None:
        config = WatchdogConfig()
    resolved_now = now or datetime.now(timezone.utc)
    # Pull a window-sized lookback from the log. We pad by 2x the
    # window so the cancel-no-promotion rule can still see the
    # ``task.created`` that resolved a cancel just outside the
    # window. The detectors filter further by timestamp.
    since = (
        resolved_now - timedelta(seconds=config.window_seconds * 2)
    ).isoformat()
    events = read_events(
        project,
        since=since,
        project_path=project_path,
    )
    return scan_events(
        events,
        now=resolved_now,
        config=config,
        open_tasks=open_tasks,
        storage_window_names=storage_window_names,
        project=project,
        done_plan_tasks=done_plan_tasks,
        plan_review_present=plan_review_present,
        legacy_db_shadows=legacy_db_shadows,
        plan_missing_clears=plan_missing_clears,
        state_db_probes=state_db_probes,
        run_safety_net_probes=run_safety_net_probes,
    )


# ---------------------------------------------------------------------------
# Emit helpers — one for the heartbeat self-check, one for findings
# ---------------------------------------------------------------------------


def emit_heartbeat_tick(
    *,
    project: str = "",
    actor: str = "audit_watchdog",
    metadata: dict[str, Any] | None = None,
    project_path: Path | str | None = None,
) -> None:
    """Emit a ``heartbeat.tick`` audit event so we can prove liveness.

    Called by the cadence handler at the start of every scan. An
    operator (or a future ``pm doctor``) can grep the central tail
    for ``heartbeat.tick`` and observe the cadence — if the gaps grow,
    the heartbeat itself is wedged.
    """
    from pollypm.audit.log import emit as _audit_emit
    try:
        _audit_emit(
            event=EVENT_HEARTBEAT_TICK,
            project=project,
            subject="audit_watchdog",
            actor=actor,
            metadata=dict(metadata or {}),
            project_path=project_path,
        )
    except Exception:  # noqa: BLE001 — never fail the cadence on audit hiccups
        logger.debug("emit_heartbeat_tick failed", exc_info=True)


def emit_finding(finding: Finding) -> None:
    """Emit an ``audit.finding`` event for posterity.

    The cadence handler also routes findings to ``upsert_alert`` for
    user-visible surfacing; this audit emit is the durable forensic
    trail. Best-effort — never raises.
    """
    from pollypm.audit.log import emit as _audit_emit
    try:
        _audit_emit(
            event=EVENT_AUDIT_FINDING,
            project=finding.project,
            subject=finding.subject,
            actor="audit_watchdog",
            status=finding.severity,
            metadata={
                "rule": finding.rule,
                "message": finding.message,
                "recommendation": finding.recommendation,
                **dict(finding.metadata or {}),
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug("emit_finding failed", exc_info=True)


# ---------------------------------------------------------------------------
# Auto-unstick dispatch (#1414)
# ---------------------------------------------------------------------------


def was_recently_dispatched(
    *,
    project: str,
    finding_type: str,
    subject: str,
    now: datetime,
    project_path: Path | str | None = None,
    throttle_seconds: int = ESCALATION_THROTTLE_SECONDS,
) -> bool:
    """Return True iff the audit log shows a matching dispatch in-window.

    The throttle uses the audit log itself as the source of truth so a
    heartbeat-process restart doesn't reset the dedup window. We scan
    ``EVENT_WATCHDOG_ESCALATION_DISPATCHED`` for the project in the last
    ``throttle_seconds`` and look for a row whose metadata.finding_type
    and subject both match.
    """
    from pollypm.audit.log import read_events

    cutoff = (now - timedelta(seconds=throttle_seconds)).isoformat()
    try:
        recent = read_events(
            project,
            since=cutoff,
            event=EVENT_WATCHDOG_ESCALATION_DISPATCHED,
            project_path=project_path,
        )
    except Exception:  # noqa: BLE001 — never block dispatch on read failure
        logger.debug(
            "was_recently_dispatched: read_events failed", exc_info=True,
        )
        return False
    for ev in recent:
        meta = ev.metadata or {}
        if (
            meta.get("finding_type") == finding_type
            and (ev.subject == subject or meta.get("subject") == subject)
        ):
            return True
    return False


def emit_escalation_dispatched(
    *,
    project: str,
    finding_type: str,
    subject: str,
    brief: str,
    project_path: Path | str | None = None,
) -> None:
    """Emit a ``watchdog.escalation_dispatched`` event.

    The metadata carries enough to reconstruct the dispatch decision:
    finding_type, subject, and the brief that was sent. Best-effort —
    never raises.
    """
    from pollypm.audit.log import emit as _audit_emit
    try:
        _audit_emit(
            event=EVENT_WATCHDOG_ESCALATION_DISPATCHED,
            project=project,
            subject=subject,
            actor="audit_watchdog",
            status="warn",
            metadata={
                "finding_type": finding_type,
                "subject": subject,
                "brief": brief,
            },
            project_path=project_path,
        )
    except Exception:  # noqa: BLE001
        logger.debug("emit_escalation_dispatched failed", exc_info=True)


def was_recently_operator_dispatched(
    *,
    project: str,
    finding_type: str,
    subject: str,
    now: datetime,
    project_path: Path | str | None = None,
    throttle_seconds: int = OPERATOR_DISPATCH_THROTTLE_SECONDS,
) -> bool:
    """Return True iff a matching tier-3 dispatch landed in the throttle window.

    Mirrors :func:`was_recently_dispatched` but reads
    ``EVENT_WATCHDOG_OPERATOR_DISPATCHED`` rows. Same idempotence
    model — the audit log itself is the source of truth, so a
    heartbeat-process restart doesn't reset the dedup window.
    """
    from pollypm.audit.log import read_events

    cutoff = (now - timedelta(seconds=throttle_seconds)).isoformat()
    try:
        recent = read_events(
            project,
            since=cutoff,
            event=EVENT_WATCHDOG_OPERATOR_DISPATCHED,
            project_path=project_path,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "was_recently_operator_dispatched: read_events failed",
            exc_info=True,
        )
        return False
    for ev in recent:
        meta = ev.metadata or {}
        if (
            meta.get("finding_type") == finding_type
            and (ev.subject == subject or meta.get("subject") == subject)
        ):
            return True
    return False


def emit_operator_dispatched(
    *,
    project: str,
    finding_type: str,
    subject: str,
    inbox_task_id: str | None = None,
    dedup_key: str = "",
    project_path: Path | str | None = None,
) -> None:
    """Emit a ``watchdog.operator_dispatched`` event.

    Symmetric to :func:`emit_escalation_dispatched` but for the
    tier-3 (operator) leg of the cascade. Best-effort — never raises.
    """
    from pollypm.audit.log import emit as _audit_emit
    try:
        _audit_emit(
            event=EVENT_WATCHDOG_OPERATOR_DISPATCHED,
            project=project,
            subject=subject,
            actor="audit_watchdog",
            status="warn",
            metadata={
                "finding_type": finding_type,
                "subject": subject,
                "inbox_task_id": inbox_task_id or "",
                "dedup_key": dedup_key,
            },
            project_path=project_path,
        )
    except Exception:  # noqa: BLE001
        logger.debug("emit_operator_dispatched failed", exc_info=True)


# ---------------------------------------------------------------------------
# Tier handoff prompt builder (#1546)
# ---------------------------------------------------------------------------


# Reserved keys that callers MUST NOT pass through ``evidence`` because
# they would re-introduce solution-menu language. The lint test asserts
# this set is honored at runtime.
_FORBIDDEN_EVIDENCE_KEYS: frozenset[str] = frozenset({
    "options", "solutions", "candidates", "choices", "menu",
    "recommended_actions", "suggested_actions",
})


class SolutionMenuError(ValueError):
    """Raised when a tier-handoff prompt builder is asked to embed
    candidate solutions in its evidence section.

    The cascade is a model-capability ladder. Pre-loading the upstairs
    reasoner with the downstairs hypothesis set caps the upstairs at
    that hypothesis space, defeating the escalation. Tier-2/3/4
    prompts must hand structured evidence and a question — never a
    menu of solutions. The builder refuses any evidence dict that
    looks like a solution menu so the constraint can't drift in
    callers.
    """


def tier_handoff_prompt(evidence: dict[str, Any], question: str) -> str:
    """Render a structured-evidence handoff prompt for tier-2/3/4 dispatch.

    Builder shape — the function intentionally accepts ONLY ``evidence``
    and ``question``. There is no ``solutions`` parameter and the
    evidence dict is structurally rejected if it carries solution-menu
    keys (see :data:`_FORBIDDEN_EVIDENCE_KEYS`).

    Format:

        TIER HANDOFF

        Question: <one-line question>

        Evidence:
        - <key>: <value>
        - <key>: <value>
          - <subkey>: <subvalue>

    Lists are rendered as nested bullets. Dicts nest one level deep
    (deeper structure is fine — the renderer reflects whatever shape
    is passed in). The format is a paste-friendly text block; the
    architect / operator pane reads it as the agent's user-turn
    content, so we keep it ASCII and avoid markdown headers that
    different agents render differently.

    The lint test in ``tests/test_audit_watchdog.py`` exercises a
    representative evidence dict for every rule and asserts the
    rendered prompt contains no solution-menu language.
    """
    if not isinstance(evidence, dict):
        raise SolutionMenuError(
            "tier_handoff_prompt: evidence must be a dict, got "
            f"{type(evidence).__name__}",
        )
    bad_keys = sorted(set(evidence.keys()) & _FORBIDDEN_EVIDENCE_KEYS)
    if bad_keys:
        raise SolutionMenuError(
            "tier_handoff_prompt: evidence contains solution-menu "
            f"keys {bad_keys}. The cascade is a model-capability "
            "ladder; the upstairs reasoner gets evidence and a "
            "question, never a pre-loaded solution space."
        )
    if not isinstance(question, str) or not question.strip():
        raise SolutionMenuError(
            "tier_handoff_prompt: question must be a non-empty string",
        )

    lines: list[str] = ["TIER HANDOFF", ""]
    lines.append(f"Question: {question.strip()}")
    lines.append("")
    lines.append("Evidence:")
    if not evidence:
        lines.append("- (none)")
        return "\n".join(lines)

    def _render_value(value: Any, indent: int) -> list[str]:
        prefix = "  " * indent
        out: list[str] = []
        if isinstance(value, dict):
            if not value:
                out.append(f"{prefix}(empty)")
                return out
            for k, v in value.items():
                if isinstance(v, (dict, list, tuple)):
                    out.append(f"{prefix}- {k}:")
                    out.extend(_render_value(v, indent + 1))
                else:
                    out.append(f"{prefix}- {k}: {v}")
        elif isinstance(value, (list, tuple)):
            if not value:
                out.append(f"{prefix}(empty)")
                return out
            for entry in value:
                if isinstance(entry, (dict, list, tuple)):
                    out.append(f"{prefix}-")
                    out.extend(_render_value(entry, indent + 1))
                else:
                    out.append(f"{prefix}- {entry}")
        else:
            out.append(f"{prefix}{value}")
        return out

    for key, value in evidence.items():
        if isinstance(value, (dict, list, tuple)):
            lines.append(f"- {key}:")
            lines.extend(_render_value(value, 1))
        else:
            lines.append(f"- {key}: {value}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Urgent-inbox structured-message helper (#1546 terminal handoff)
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class InboxItemBuilder:
    """Pre-built urgent-inbox handoff payload.

    Returned by :func:`build_urgent_human_handoff`. The cadence handler
    converts this into a ``pm notify``-shaped row when the cascade has
    exhausted automated options. Carries enough to render the inbox
    card (subject, body, labels, priority) without re-deriving from
    the failed-attempt history.

    Today the helper is wired but not auto-triggered — the explicit
    rule covering "Polly has exhausted authority" lives in a follow-up
    PR. Shipping the helper + tests now means the terminal-handoff
    surface is a stable target for that future rule.
    """

    subject: str
    body: str
    labels: tuple[str, ...]
    priority: str = "immediate"
    requester: str = "user"
    actor: str = "audit_watchdog"
    project: str = ""
    forensics_path: str = ""


def build_urgent_human_handoff(
    *,
    tried: Sequence[str],
    failed: Sequence[str],
    hypothesis: str,
    forensics_path: str,
    project: str = "",
    subject_hint: str = "",
) -> InboxItemBuilder:
    """Compose an urgent-inbox handoff message.

    The message body opens with ``hypothesis`` (one paragraph the
    operator reads first), then lists what was tried and what failed,
    and ends with the forensics path so the user can drill in. The
    inbox item is tagged ``urgent`` and ``notify`` so the cockpit
    surface lifts it above the regular queue.

    The helper enforces non-empty hypothesis / forensics_path so a
    caller can't ship a card that's structurally indistinguishable
    from a normal notify; user-blocking is the very last resort and
    the body has to carry the load when it lands.
    """
    if not isinstance(hypothesis, str) or not hypothesis.strip():
        raise ValueError(
            "build_urgent_human_handoff: hypothesis must be a "
            "non-empty paragraph — the operator reads this first.",
        )
    if not isinstance(forensics_path, str) or not forensics_path.strip():
        raise ValueError(
            "build_urgent_human_handoff: forensics_path must be a "
            "non-empty path — the operator drills in via this link.",
        )

    subject_text = subject_hint.strip() or (
        f"Cascade exhausted for {project or 'workspace'} — operator decision needed"
    )

    body_lines: list[str] = []
    body_lines.append(hypothesis.strip())
    body_lines.append("")
    if tried:
        body_lines.append("What was tried:")
        for item in tried:
            body_lines.append(f"- {item}")
        body_lines.append("")
    if failed:
        body_lines.append("What failed:")
        for item in failed:
            body_lines.append(f"- {item}")
        body_lines.append("")
    body_lines.append(f"Forensics: {forensics_path.strip()}")
    body = "\n".join(body_lines).strip()

    labels: tuple[str, ...] = ("urgent", "notify")
    return InboxItemBuilder(
        subject=subject_text,
        body=body,
        labels=labels,
        priority="immediate",
        requester="user",
        actor="audit_watchdog",
        project=project,
        forensics_path=forensics_path.strip(),
    )


def format_unstick_brief(finding: Finding) -> str:
    """Render a finding as a structured brief for the architect.

    Each rule produces a tailored brief — the framing is the same
    (``WATCHDOG ESCALATION`` header + structured fields + decision
    options) but the ``Observed evidence`` and ``Your job`` lines vary
    so the architect knows which lever to pull first.
    """
    project = finding.project or "<unknown>"
    subject = finding.subject or "<unknown>"
    meta = finding.metadata or {}

    lines: list[str] = ["WATCHDOG ESCALATION", ""]
    lines.append(f"Project: {project}")
    lines.append(f"Finding: {finding.rule}")
    lines.append(f"Subject: {subject}")

    if finding.rule == RULE_TASK_ON_HOLD_STALE:
        stuck_minutes = meta.get("stuck_minutes")
        on_hold_since = meta.get("on_hold_since")
        from_state = meta.get("from") or "<unknown>"
        reason = meta.get("reason")
        routing = meta.get("routing") or ON_HOLD_ARCHITECT_TAG
        reviewer_evidence = meta.get("reviewer_evidence") or []
        lines.append(
            f"Stuck for: {stuck_minutes} minutes" if stuck_minutes
            else "Stuck for: unknown duration"
        )
        lines.append(f"Routing: {routing}")
        lines.append("Observed evidence:")
        if on_hold_since:
            lines.append(
                f"- Task transitioned {from_state} -> on_hold at {on_hold_since}"
            )
        if reviewer_evidence:
            lines.append(
                "- Recent reviewer/inbox rationale evidence "
                "(authoritative if it differs from the transition reason):"
            )
            for entry in reviewer_evidence:
                # Each entry is a one-line string already shaped by
                # the cadence handler (exec row OR inbox message).
                lines.append(f"  * {entry}")
        else:
            lines.append(
                "- No additional reviewer execution rows or inbox "
                "messages were available."
            )
        if reason:
            lines.append(f"- On-hold transition reason: {reason}")
        else:
            lines.append("- No transition reason was recorded.")
        lines.append("")
        lines.append(
            "Your job (DEFAULT: fix and re-submit). Read the evidence "
            "above and generate hypotheses fresh from the reviewer's "
            "findings — do NOT ratify any framing in this brief."
        )
        lines.append(
            f"Cli levers available: `pm task queue {subject}`, "
            f"`pm task approve {subject}`, `pm notify --priority immediate`. "
            "Parking on the user is the failure mode this rule exists "
            "to prevent."
        )
    elif finding.rule == RULE_TASK_REVIEW_STALE:
        stuck_minutes = meta.get("stuck_minutes")
        review_since = meta.get("review_since")
        lines.append(
            f"Stuck for: {stuck_minutes} minutes" if stuck_minutes
            else "Stuck for: unknown duration"
        )
        lines.append("Observed evidence:")
        if review_since:
            lines.append(f"- Task transitioned to status=review at {review_since}")
        lines.append(
            "- No subsequent task.status_changed for this subject"
        )
        lines.append(
            "- Reviewer agent appears to be absent or stuck"
        )
        lines.append("")
        lines.append(
            "Your job: investigate the evidence above and unstick the "
            "task. Generate hypotheses fresh from the data — do NOT "
            "ratify the framing in this brief."
        )
        lines.append(
            f"Cli levers available: `pm task done {subject}`, "
            "`pm chat <project> --role reviewer`, `pm notify`."
        )
    elif finding.rule == RULE_TASK_PROGRESS_STALE:
        stuck_minutes = meta.get("stuck_minutes")
        in_progress_minutes = meta.get("in_progress_minutes")
        in_progress_since = meta.get("in_progress_since")
        last_activity_at = meta.get("last_activity_at")
        last_activity_kind = meta.get("last_activity_kind") or "<unknown>"
        assignee = meta.get("assignee") or "<unknown>"
        node = meta.get("current_node_id") or "<unknown>"
        lines.append(
            f"Stuck for: {stuck_minutes} minutes without progress activity"
            if stuck_minutes else "Stuck for: unknown duration"
        )
        if in_progress_minutes:
            lines.append(f"In progress for: {in_progress_minutes} minutes")
        lines.append("Observed evidence:")
        if in_progress_since:
            lines.append(
                f"- Task transitioned to status=in_progress at {in_progress_since}"
            )
        if last_activity_at:
            lines.append(
                f"- Last progress signal: {last_activity_kind} at {last_activity_at}"
            )
        lines.append(f"- Assignee: {assignee}; node: {node}")
        lines.append(
            "- Worker pane may be alive but unproductive: common causes "
            "include auth failure, sandbox denial, quota/capacity, or "
            "worker logic stuck after a nudge."
        )
        lines.append("")
        lines.append(
            "Your job: investigate the evidence above and unstick the "
            "task. Auth / sandbox / quota / worker logic are the usual "
            "root-cause families — generate hypotheses fresh from the "
            "evidence rather than ratifying any framing in this brief."
        )
        lines.append(
            f"Cli levers available: `pm task cancel {subject}`, "
            "`pm chat <project>`, `pm notify`."
        )
    elif finding.rule == RULE_ROLE_SESSION_MISSING:
        expected = meta.get("expected_window") or "<unknown>"
        role = meta.get("role") or "<unknown>"
        status = meta.get("status") or "<unknown>"
        lines.append("Stuck for: a watchdog cycle (>= 5 minutes)")
        lines.append("Observed evidence:")
        lines.append(f"- Task at status={status} with role={role}")
        lines.append(
            f"- No '{expected}' window in the storage-closet "
            f"tmux session"
        )
        lines.append(
            "- Without the role session, the task cannot make progress"
        )
        lines.append("")
        lines.append(
            "Your job: spawn the missing role lane or reassign the task. "
            "Generate hypotheses fresh from the evidence — do NOT ratify "
            "the framing in this brief."
        )
        lines.append(
            f"Cli levers available: `pm chat {project} --role {role}`, "
            "`pm notify`."
        )
    elif finding.rule == RULE_PLAN_REVIEW_MISSING:
        plan_task_id = meta.get("plan_task_id") or subject
        flow_id = meta.get("flow_template_id") or "<unknown>"
        labels = meta.get("labels") or []
        created_at = meta.get("created_at")
        lines.append("Stuck for: plan_review never emitted")
        lines.append("Observed evidence:")
        lines.append(
            f"- Plan-shaped task {plan_task_id} reached done on flow={flow_id}"
        )
        if labels:
            label_preview = ", ".join(str(label) for label in labels[:6])
            lines.append(f"- Labels: {label_preview}")
        if created_at:
            lines.append(f"- Created at: {created_at}")
        lines.append(
            "- The cockpit's plan_review approval card needs a "
            "messages-table row with labels=[plan_review, "
            f"plan_task:{plan_task_id}] but none exists."
        )
        lines.append("")
        lines.append(
            "Your job: the watchdog will backfill the plan_review row on "
            "this tick via plan_review_emit.emit_plan_review_for_task. "
            "No manual action required unless the backfill itself fails "
            "in the cadence logs."
        )
    elif finding.rule == RULE_REJECTION_LOOP:
        node_id = (finding.evidence or {}).get("node_id") or meta.get(
            "node_id",
        ) or "<unknown>"
        reject_count = (
            (finding.evidence or {}).get("reject_count")
            or meta.get("reject_count")
            or "?"
        )
        attempts = (finding.evidence or {}).get("attempts") or []
        shared_tokens = (finding.evidence or {}).get("shared_tokens") or []
        window_seconds = (
            (finding.evidence or {}).get("window_seconds")
            or meta.get("window_seconds")
            or REJECTION_LOOP_WINDOW_SECONDS
        )
        try:
            window_minutes = max(1, int(window_seconds) // 60)
        except (TypeError, ValueError):
            window_minutes = 120
        lines.append(
            f"Stuck for: {reject_count} rejections at node '{node_id}' in the "
            f"last {window_minutes} min"
        )
        lines.append("Observed evidence:")
        for entry in attempts:
            if not isinstance(entry, dict):
                continue
            attempt_node = entry.get("node") or "?"
            completed = entry.get("completed_at") or "?"
            reason = entry.get("reason") or ""
            line = (
                f"- attempt at {attempt_node} @ {completed}"
            )
            if reason:
                clipped = reason.strip().splitlines()[0][:240]
                line = f"{line} reason: {clipped}"
            lines.append(line)
        if shared_tokens:
            lines.append(
                "- Pattern across rejections (shared tokens): "
                f"{', '.join(str(t) for t in shared_tokens[:8])}"
            )
        lines.append("")
        lines.append(
            "Your job: this task is in a structural rejection loop. The "
            "worker can't escape its dependency-and-runtime context "
            "locally. Generate hypotheses fresh from the evidence above "
            "rather than ratifying any framing in this brief."
        )
        lines.append(
            "Decisions in scope: retry-with-structural-change, "
            "restructure-the-work-itself (cancel + recreate with a "
            "different approach), or escalate-to-Polly. Do NOT "
            "auto-cancel — let the PM choose."
        )
    elif finding.rule == RULE_WORKER_SESSION_DEAD_LOOP:
        reap_count = meta.get("reap_count") or "?"
        latest_reason = meta.get("latest_reason") or "<unknown>"
        lines.append(
            f"Stuck for: {reap_count} reaps in the last 10 minutes"
        )
        lines.append("Observed evidence:")
        lines.append(
            f"- Worker session for {subject} reaped {reap_count} times"
        )
        lines.append(f"- Most recent reaper reason: {latest_reason}")
        lines.append(
            "- The reaper is firing in a loop — the session keeps "
            "dying after spawn"
        )
        lines.append("")
        lines.append(
            "Your job: investigate the evidence above and unstick the "
            "task. The reaper firing in a loop is a session-spawn root "
            "cause — generate hypotheses fresh from the evidence rather "
            "than ratifying any framing in this brief."
        )
        lines.append(
            f"Cli levers available: `pm task cancel {subject}`, `pm notify`."
        )
    else:
        # Fallback: orphan_marker / marker_leaked / stuck_draft / cancel_no_promotion
        # all have human-readable messages already; we re-use those.
        lines.append("Stuck for: see message")
        lines.append("Observed evidence:")
        if finding.message:
            lines.append(f"- {finding.message}")
        if finding.recommendation:
            lines.append(f"- Recommendation: {finding.recommendation}")
        lines.append("")
        lines.append(
            "Your job: investigate the evidence above and unstick the "
            "task. Generate hypotheses fresh from the evidence rather "
            "than ratifying the recommendation."
        )
        lines.append(
            "Cli levers available: act on the recommendation above, take "
            "a different action you judge appropriate, or escalate to "
            "user via `pm notify`."
        )
    return "\n".join(lines)
