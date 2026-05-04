"""``blocked_chain.sweep`` — auto-escalate recursively-blocked tasks (#1073).

Tasks marked ``blocked`` because their dependencies are themselves
unfinished (and have no work in flight) used to sit silently forever.
The PM / architect never noticed; ``pm alerts`` never surfaced anything;
the operator was the only escalation path. This sweep walks each
project's blocker graph, detects "blocked recursively with no in-flight
work in the chain", and emits a single deduped ``blocked_dead_end``
alert per (project, task) so the architect / operator see the stuck
chain instead of having to spot-check the blocked list.

Detection contract (kept deliberately minimal — no scope creep on
auto-replan; the alert is the surface, the architect/PM session reads
it and decides):

* The task itself has been in ``blocked`` for ≥ ``stale_threshold_seconds``
  (default 1 hour). Latest ``work_transitions`` row with
  ``to_state='blocked'`` provides the timestamp; tasks whose row was
  never transitioned (e.g. legacy / hand-edited) fall back to
  ``updated_at`` and finally to ``created_at``.
* Every recursive blocker (``blocked_by`` chain via the ``blocks`` link
  kind in ``work_task_dependencies``) is in a non-terminal,
  non-in-flight status. "In-flight" means ``in_progress`` / ``review`` /
  ``rework`` — actively being worked on or actively being reviewed.
  ``queued`` counts as not-in-flight: a queued blocker that itself
  depends on stuck work is still part of a dead end. ``done`` /
  ``cancelled`` blockers are filtered upstream by ``maybe_unblock``;
  if any remain on the chain we still treat them as resolved.

The alert is keyed by ``session_name = blocked-<project>-<N>`` and
``alert_type = blocked_dead_end`` so ``upsert_alert`` dedupes across
sweep ticks. Cleared automatically when the task leaves ``blocked``
(handled by the work-service's existing alert hygiene paths and by
this sweep clearing rows whose tasks are no longer blocked).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pollypm.work.models import WorkStatus


logger = logging.getLogger(__name__)


# Default: a task must have been blocked for at least this long before
# the sweep escalates. One hour balances "actually stuck" against
# "blocker just got marked done — give the auto-unblock a beat to
# resolve". Tunable via the handler payload.
DEFAULT_STALE_THRESHOLD_SECONDS: int = 3600

# Statuses that count as "in-flight work" — a blocker in any of these
# is being actively progressed, so the chain is not a dead end.
_IN_FLIGHT_STATUSES: frozenset[str] = frozenset({
    WorkStatus.IN_PROGRESS.value,
    WorkStatus.REVIEW.value,
    WorkStatus.REWORK.value,
})

# Statuses that count as resolved — a blocker that's done or cancelled
# would already have been filtered out by ``maybe_unblock``; if it
# lingers we still treat it as not-blocking.
_RESOLVED_STATUSES: frozenset[str] = frozenset({
    WorkStatus.DONE.value,
    WorkStatus.CANCELLED.value,
})


def blocked_dead_end_session_name(project: str, task_number: int) -> str:
    """Return the synthetic session name for a ``blocked_dead_end`` alert."""
    return f"blocked-{project}-{int(task_number)}"


BLOCKED_DEAD_END_ALERT_TYPE = "blocked_dead_end"


def _parse_iso(stamp: Any) -> datetime | None:
    if stamp is None:
        return None
    if isinstance(stamp, datetime):
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        dt = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _blocked_since(work: Any, project: str, task_number: int) -> datetime | None:
    """Return the timestamp the task most recently entered ``blocked``.

    Falls back to ``updated_at`` / ``created_at`` when no transition row
    exists. Returns ``None`` only if every candidate is unparseable.
    """
    from pollypm.work.task_state import blocked_since_stamp_for_service

    stamp = blocked_since_stamp_for_service(
        work,
        project_key=project,
        task_number=task_number,
        blocked_status=WorkStatus.BLOCKED.value,
    )
    return _parse_iso(stamp)


def _walk_blocker_chain(
    work: Any,
    *,
    project: str,
    task_number: int,
) -> tuple[set[tuple[str, int]], dict[tuple[str, int], str]]:
    """Walk the recursive ``blocked_by`` chain.

    Returns ``(visited, status_by_key)``: the full set of ancestor
    blocker keys (excluding the originating task) and a map of each
    blocker's current ``work_status`` value. Blockers whose row is
    missing from ``work_tasks`` map to the empty string so the caller
    can treat them as unresolved (the dependency exists but the task
    record is gone).
    """
    from pollypm.work.task_state import blocker_chain_for_service

    return blocker_chain_for_service(
        work,
        project_key=project,
        task_number=task_number,
    )


def is_dead_end_chain(status_by_key: dict[tuple[str, int], str]) -> bool:
    """Return True when no blocker in the chain has in-flight work.

    A chain is "dead end" iff:
    * It is non-empty (the task has at least one recursive blocker), AND
    * No blocker is in ``in_progress`` / ``review`` / ``rework``.

    Resolved blockers (``done`` / ``cancelled``) and unresolved-but-not-
    in-flight states (``draft`` / ``queued`` / ``blocked`` / ``on_hold``
    / ``rework``-but-no-active-claim) all count as "no work in flight".
    """
    if not status_by_key:
        return False
    for status in status_by_key.values():
        if status in _IN_FLIGHT_STATUSES:
            return False
    return True


def _format_chain_summary(
    *,
    task_id: str,
    visited: set[tuple[str, int]],
    status_by_key: dict[tuple[str, int], str],
) -> str:
    """Build the human-readable alert message body."""
    blocker_count = len(visited)
    by_status: dict[str, int] = {}
    for status in status_by_key.values():
        bucket = status or "unknown"
        by_status[bucket] = by_status.get(bucket, 0) + 1
    breakdown = ", ".join(
        f"{count} {label}"
        for label, count in sorted(by_status.items())
    )
    word = "blocker" if blocker_count == 1 else "blockers"
    return (
        f"Task {task_id} is stuck on a dead-end dependency chain: "
        f"{blocker_count} recursive {word} ({breakdown}) and none are "
        f"in flight. Re-plan the chain (start the earliest blocker, "
        f"descope, or escalate) — no worker can pick this up until "
        f"the chain unblocks."
    )


def _emit_alert(
    *,
    msg_store: Any,
    state_store: Any,
    project: str,
    task_number: int,
    message: str,
) -> bool:
    """Upsert a ``blocked_dead_end`` alert. Returns True iff a write ran."""
    session_name = blocked_dead_end_session_name(project, task_number)
    target = msg_store or state_store
    if target is None:
        return False
    try:
        target.upsert_alert(
            session_name, BLOCKED_DEAD_END_ALERT_TYPE, "warn", message,
        )
        return True
    except Exception:  # noqa: BLE001
        logger.debug(
            "blocked_chain.sweep: upsert_alert failed for %s/%d",
            project, task_number, exc_info=True,
        )
        return False


def _clear_alert(
    *,
    msg_store: Any,
    state_store: Any,
    project: str,
    task_number: int,
) -> bool:
    """Clear the ``blocked_dead_end`` alert for a (project, task) pair."""
    session_name = blocked_dead_end_session_name(project, task_number)
    target = msg_store or state_store
    if target is None:
        return False
    try:
        target.clear_alert(session_name, BLOCKED_DEAD_END_ALERT_TYPE)
        return True
    except Exception:  # noqa: BLE001
        logger.debug(
            "blocked_chain.sweep: clear_alert failed for %s/%d",
            project, task_number, exc_info=True,
        )
        return False


def sweep_blocked_chains(
    *,
    work: Any,
    msg_store: Any,
    state_store: Any,
    now: datetime,
    stale_threshold_seconds: int = DEFAULT_STALE_THRESHOLD_SECONDS,
) -> dict[str, int]:
    """Run one sweep against a single work-service DB. Pure helper.

    Walks every blocked task, detects dead-end chains older than
    ``stale_threshold_seconds``, and emits one ``blocked_dead_end``
    alert per offending task. Returns counters for observability.
    """
    counters = {
        "blocked_considered": 0,
        "dead_end_detected": 0,
        "alerts_raised": 0,
        "skipped_recent": 0,
        "skipped_in_flight_chain": 0,
        "skipped_no_blockers": 0,
    }

    try:
        blocked = work.list_tasks(work_status=WorkStatus.BLOCKED.value)
    except Exception:  # noqa: BLE001
        logger.debug(
            "blocked_chain.sweep: list_tasks(blocked) failed", exc_info=True,
        )
        return counters

    threshold = timedelta(seconds=max(0, int(stale_threshold_seconds)))
    for task in blocked:
        counters["blocked_considered"] += 1
        project = task.project
        task_number = int(task.task_number)
        blocked_at = _blocked_since(work, project, task_number)
        if blocked_at is not None and (now - blocked_at) < threshold:
            counters["skipped_recent"] += 1
            continue
        visited, status_by_key = _walk_blocker_chain(
            work, project=project, task_number=task_number,
        )
        if not visited:
            # Task is in BLOCKED state with no recursive blocker rows —
            # nothing to dead-end against. Some other process (manual
            # state edit, sync drift) put it there; not our concern.
            counters["skipped_no_blockers"] += 1
            continue
        # Drop resolved blockers from the dead-end consideration. If
        # any remain in flight the chain is alive.
        live_chain = {
            key: status
            for key, status in status_by_key.items()
            if status not in _RESOLVED_STATUSES
        }
        if not live_chain:
            # Every blocker is done/cancelled — auto-unblock should
            # pick this up; not a dead end.
            counters["skipped_no_blockers"] += 1
            continue
        if not is_dead_end_chain(live_chain):
            counters["skipped_in_flight_chain"] += 1
            continue
        counters["dead_end_detected"] += 1
        message = _format_chain_summary(
            task_id=task.task_id,
            visited=set(live_chain.keys()),
            status_by_key=live_chain,
        )
        if _emit_alert(
            msg_store=msg_store,
            state_store=state_store,
            project=project,
            task_number=task_number,
            message=message,
        ):
            counters["alerts_raised"] += 1

    return counters


def _open_project_work(project: Any) -> Any | None:
    """Open a per-project SQLite work service. Returns None on failure."""
    from pollypm.work.service_factory import open_project_work_service

    return open_project_work_service(project)


def _close_quietly(work: Any) -> None:
    if work is None:
        return
    closer = getattr(work, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # noqa: BLE001
            pass


def blocked_chain_sweep_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Cadence handler — emit ``blocked_dead_end`` alerts (#1073).

    Fans out across the workspace-root DB and every registered
    per-project DB, calling :func:`sweep_blocked_chains` once per DB.
    Idempotent across ticks via ``upsert_alert`` dedupe.
    """
    from pollypm.runtime_services import load_runtime_services

    config_path_hint = payload.get("config_path") if isinstance(payload, dict) else None
    config_path = Path(config_path_hint) if config_path_hint else None
    services = load_runtime_services(config_path=config_path)
    stale_threshold_seconds = int(
        (payload or {}).get(
            "stale_threshold_seconds", DEFAULT_STALE_THRESHOLD_SECONDS,
        )
        or DEFAULT_STALE_THRESHOLD_SECONDS,
    )

    totals = {
        "blocked_considered": 0,
        "dead_end_detected": 0,
        "alerts_raised": 0,
        "skipped_recent": 0,
        "skipped_in_flight_chain": 0,
        "skipped_no_blockers": 0,
    }
    projects_scanned = 0

    now = datetime.now(UTC)
    try:
        if services.work_service is not None:
            partial = sweep_blocked_chains(
                work=services.work_service,
                msg_store=services.msg_store,
                state_store=services.state_store,
                now=now,
                stale_threshold_seconds=stale_threshold_seconds,
            )
            for key, value in partial.items():
                totals[key] += value
            projects_scanned += 1

        for project in services.known_projects or ():
            project_work = _open_project_work(project)
            if project_work is None:
                continue
            try:
                partial = sweep_blocked_chains(
                    work=project_work,
                    msg_store=services.msg_store,
                    state_store=services.state_store,
                    now=now,
                    stale_threshold_seconds=stale_threshold_seconds,
                )
                for key, value in partial.items():
                    totals[key] += value
                projects_scanned += 1
            finally:
                _close_quietly(project_work)
    finally:
        try:
            services.close()
        except Exception:  # noqa: BLE001
            logger.debug(
                "blocked_chain.sweep: services.close raised", exc_info=True,
            )

    return {
        "outcome": "swept",
        "projects_scanned": projects_scanned,
        **totals,
    }
