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
   with no matching ``marker.released`` within the window AND no
   ``task.status_changed`` to a terminal state (``done`` / ``cancelled``
   / ``abandoned``) in that window. Catches the savethenovel pattern
   where a worker is launched and the task is then cancelled but the
   on-disk marker lingers.
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

Severity is ``"warn"`` for every rule today. The recovery hint is
attached to each finding so the alert message can include a
copy-pasteable next step (``"run pm task promote ..."``).

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
    "scan_events",
    "scan_project",
    "emit_heartbeat_tick",
    "emit_finding",
    "watchdog_alert_session_name",
    "WATCHDOG_ALERT_TYPE",
    "format_finding_message",
]


logger = logging.getLogger(__name__)


# Stable rule names — pinned because alert dedupe uses them as part of
# the synthetic session_name key, and operators grep them in the audit
# log. New rules should follow ``noun.verb_or_state`` form.
RULE_ORPHAN_MARKER = "orphan_marker"
RULE_MARKER_LEAKED = "marker_leaked"
RULE_STUCK_DRAFT = "stuck_draft"
RULE_CANCEL_NO_PROMOTION = "cancellation_no_promotion"

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
    """Tunable thresholds for the four detection rules.

    All durations are in seconds. Defaults match the spec in the
    task brief — 30 min lookback for orphan/leak rules, 5 min for
    the cheaper draft / cancellation gates.
    """

    # Lookback for orphan-marker + leak detection.
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


@dataclass(slots=True, frozen=True)
class Finding:
    """One watchdog detection — what was wrong, where, and what to do.

    ``rule`` is one of the ``RULE_*`` constants. ``project`` and
    ``subject`` mirror the audit-event shape so a downstream alert
    can scope itself the same way other PollyPM alerts do.
    ``recommendation`` is a copy-pasteable hint (CLI command, file
    pointer, etc.) — surfaced to the user verbatim.
    """

    rule: str
    project: str
    subject: str
    severity: str = "warn"
    message: str = ""
    recommendation: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


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
    we extract the (project, task_number) and check whether either
    a ``marker.released`` for the same marker subject *or* a
    ``task.status_changed`` to a terminal state for the same task
    appears later in the window. If neither, it's an orphan.
    """
    findings: list[Finding] = []
    cutoff = now - timedelta(seconds=config.window_seconds)

    # Collect created markers within the window.
    created: list[tuple[AuditEvent, tuple[str, int]]] = []
    released_subjects: set[str] = set()
    terminal_tasks: set[tuple[str, int]] = set()

    for ev in events:
        ts = _parse_iso(ev.ts)
        if ts is None or ts < cutoff:
            continue
        if ev.event == EVENT_MARKER_CREATED and ev.status == "ok":
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
) -> list[Finding]:
    """Rule 3: ``task.created`` older than ``stuck_draft_seconds`` with
    no promotion.

    Iterates ``task.created`` events older than the threshold. For
    each, we check whether *any* ``task.status_changed`` for the
    same subject exists at all (looking at the full event list, not
    just inside the lookback window — a task that got promoted before
    the window started is fine). If no promotion is recorded, the
    draft is stuck.
    """
    findings: list[Finding] = []
    cutoff = now - timedelta(seconds=config.stuck_draft_seconds)

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
        key = _task_subject_key(ev.subject)
        if key is None:
            continue
        project, task_number = key
        findings.append(Finding(
            rule=RULE_STUCK_DRAFT,
            project=project,
            subject=ev.subject,
            message=(
                f"Draft task {ev.subject} has sat unpromoted for "
                f">{config.stuck_draft_seconds // 60} min "
                f"(created {ev.ts})."
            ),
            recommendation=(
                f"Promote with `pm task promote {ev.subject}` or "
                f"discard with `pm task cancel {ev.subject}`. "
                f"Originating actor: {ev.actor or 'unknown'}."
            ),
            metadata={
                "created_at": ev.ts,
                "actor": ev.actor,
                "title": (ev.metadata or {}).get("title"),
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
# Public API — scan_events / scan_project
# ---------------------------------------------------------------------------


def scan_events(
    events: Iterable[AuditEvent],
    *,
    now: datetime,
    config: WatchdogConfig | None = None,
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
        materialised, now=now, config=config,
    ))
    findings.extend(_detect_cancellation_no_promotion(
        materialised, now=now, config=config,
    ))
    return findings


def scan_project(
    project: str,
    *,
    project_path: Path | str | None = None,
    now: datetime | None = None,
    config: WatchdogConfig | None = None,
) -> list[Finding]:
    """Read audit events for ``project`` and scan them.

    Convenience wrapper used by the cadence handler. Pulls events
    from the per-project log when ``project_path`` exists, else
    from the central tail (mirrors :func:`pollypm.audit.read_events`
    source-preference rules).
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
    return scan_events(events, now=resolved_now, config=config)


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
