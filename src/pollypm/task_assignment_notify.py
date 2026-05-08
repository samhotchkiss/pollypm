"""Shared task-assignment notification helpers.

The real business logic (event shape, role → name convention, message
text) lives in ``pollypm.work.task_assignment``. This module wires those
events to runtime services — config loading, session service resolution,
dedupe / escalation decisions, and recurring sweep utilities.

The built-in ``task_assignment_notify`` plugin imports this shared module
for compatibility. Peer plugins and core recurring jobs import here
directly so they do not depend on a sibling plugin package (#1365).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pollypm.runtime_services import _RuntimeServices, load_runtime_services
from pollypm.work.models import ExecutionStatus, WorkStatus
from pollypm.work.task_assignment import (
    DEDUPE_WINDOW_SECONDS,
    RECENT_SWEEPER_PING_SECONDS,
    SessionRoleIndex,
    SWEEPER_COOLDOWN_SECONDS as SWEEPER_COOLDOWN_SECONDS,
    SWEEPER_PING_CONTEXT_ENTRY_TYPE,
    TaskAssignmentEvent,
    build_event_for_task,
    format_ping_for_role,
    role_candidate_names,
)

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_ABANDONMENTS = 3
SPAWN_FAILED_PERSISTENT_ALERT_TYPE = "spawn_failed_persistent"
RATE_BREAKER_WINDOW_SECONDS = 600
RATE_BREAKER_MAX_ABANDONMENTS = 5

_ACTIVE_WORKER_STATUSES = (
    WorkStatus.IN_PROGRESS.value,
    WorkStatus.REWORK.value,
)


# ---------------------------------------------------------------------------
# Notify primitive — used by both the event listener and the sweeper.
# ---------------------------------------------------------------------------


def notify(
    event: TaskAssignmentEvent,
    *,
    services: _RuntimeServices,
    throttle_seconds: int = DEDUPE_WINDOW_SECONDS,
    atomic_dedupe_seconds: int | None = None,
    dedupe_scope: str = "normal",
) -> dict[str, Any]:
    """Resolve + dedupe + send a single assignment ping.

    Returns a small dict describing the outcome so handlers can include
    it in their job-result payload. Never raises — every error path is
    classified (``"no_session"``, ``"deduped"``, ``"send_failed"``,
    ``"no_session_service"``).
    """
    session_svc = services.session_service
    store = services.state_store
    msg_store = services.msg_store

    if session_svc is None:
        _escalate_no_session_service(event, msg_store or store)
        return {"outcome": "no_session_service", "task_id": event.task_id}

    index = SessionRoleIndex(session_svc, work_service=services.work_service)
    # #921: pass ``task_number`` so the resolver picks up post-#919
    # per-task worker windows (``task-<project>-<N>``) in addition to
    # the legacy ``worker-<project>`` / ``worker_<project>`` long-lived
    # sessions.
    handle = index.resolve(
        event.actor_type,
        event.actor_name,
        event.project,
        task_number=event.task_number,
    )

    if handle is None:
        _escalate_no_session(event, msg_store or store, services=services)
        return {
            "outcome": "no_session",
            "task_id": event.task_id,
            "actor_type": event.actor_type.value,
            "actor_name": event.actor_name,
        }

    target_name = getattr(handle, "name", "")

    # #279: key the dedupe on ``(session, task, execution_version)``.
    # A rejection that bounces the task back to an earlier node opens a
    # fresh ``work_node_executions.visit`` — that shows up here as a new
    # ``execution_version`` and correctly lets the retry ping through
    # even inside the 30-minute window that originally throttled the
    # first ping at ``visit=1``. Events with no version (``0``) still
    # dedupe against pre-migration rows (column DEFAULT 0), preserving
    # the original throttle semantics across the upgrade.
    execution_version = int(getattr(event, "execution_version", 0) or 0)

    message = format_ping_for_role(event)

    # #952: dedupe the slot atomically BEFORE the send, not after. The
    # legacy flow was [check was_notified_within → send → record_notification],
    # which let concurrent sweep ticks all see "not yet sent" and each fire.
    # ``atomic_dedupe_seconds`` lets forced-kickoff callers bypass stale
    # historical rows while still deduping same-window concurrent sends.
    notification_id: int | None = None
    claim_window_seconds = (
        throttle_seconds if throttle_seconds > 0 else atomic_dedupe_seconds
    )
    can_claim = (
        store is not None
        and claim_window_seconds is not None
        and claim_window_seconds > 0
        and hasattr(store, "claim_notification_slot")
    )
    if can_claim:
        try:
            notification_id = store.claim_notification_slot(
                session_name=target_name,
                task_id=event.task_id,
                window_seconds=claim_window_seconds,
                execution_version=execution_version,
                project=event.project,
                message=message,
                dedupe_scope=dedupe_scope,
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "task_assignment_notify: claim_notification_slot failed for %s",
                event.task_id, exc_info=True,
            )
            notification_id = None
        else:
            if notification_id is None:
                return {
                    "outcome": "deduped",
                    "task_id": event.task_id,
                    "session": target_name,
                    "execution_version": execution_version,
                }

    if notification_id is None and store is not None and throttle_seconds > 0:
        # Legacy fallback for stores without the atomic claim helper, and for
        # transient claim-helper failures. A failed claim must not masquerade
        # as a dedupe hit — that silently drops the kickoff/resume ping.
        try:
            if store.was_notified_within(
                target_name,
                event.task_id,
                throttle_seconds,
                execution_version,
            ):
                return {
                    "outcome": "deduped",
                    "task_id": event.task_id,
                    "session": target_name,
                    "execution_version": execution_version,
                }
        except Exception:  # noqa: BLE001
            logger.debug(
                "task_assignment_notify: dedupe check failed for %s",
                event.task_id, exc_info=True,
            )

    # Clear any prior "no session" alert for this task — the recipient
    # is back online. #349: writers land in ``messages`` via the Store.
    if msg_store is not None:
        try:
            msg_store.clear_alert("task_assignment", _alert_type_for(event))
        except Exception:  # noqa: BLE001
            pass
        # #921: also clear the sweep-level ``(worker-<project>, no_session)``
        # alert raised by ``_emit_no_session_alert``. That alert is
        # keyed by the candidate session name we *would* expect, not by
        # the actual matched name (which for a per-task session is
        # ``task-<project>-<N>``), so we walk the role candidates.
        from pollypm.work.models import ActorType as _ActorType

        if event.actor_type is _ActorType.ROLE:
            from pollypm.work.task_assignment import role_candidate_names

            for candidate in role_candidate_names(
                event.actor_name, event.project,
            ):
                try:
                    msg_store.clear_alert(candidate, "no_session")
                except Exception:  # noqa: BLE001
                    pass

    try:
        session_svc.send(target_name, message)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "task_assignment_notify: send to %s failed: %s", target_name, exc,
        )
        if store is not None:
            failure_status = f"failed: {exc}"[:200]
            if notification_id is not None:
                try:
                    store.update_notification_status(
                        notification_id,
                        delivery_status=failure_status,
                    )
                except Exception:  # noqa: BLE001
                    pass
            else:
                try:
                    store.record_notification(
                        session_name=target_name,
                        task_id=event.task_id,
                        project=event.project,
                        message=message,
                        delivery_status=failure_status,
                        execution_version=execution_version,
                    )
                except Exception:  # noqa: BLE001
                    pass
        return {
            "outcome": "send_failed",
            "task_id": event.task_id,
            "session": target_name,
            "error": str(exc),
            "execution_version": execution_version,
        }

    if store is not None:
        if notification_id is not None:
            try:
                store.update_notification_status(
                    notification_id,
                    delivery_status="sent",
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "task_assignment_notify: update_notification_status failed for %s",
                    event.task_id, exc_info=True,
                )
        else:
            try:
                store.record_notification(
                    session_name=target_name,
                    task_id=event.task_id,
                    project=event.project,
                    message=message,
                    delivery_status="sent",
                    execution_version=execution_version,
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "task_assignment_notify: record_notification failed for %s",
                    event.task_id, exc_info=True,
                )

    # #923: ``notify()`` deliberately does NOT stamp ``kickoff_sent_at``
    # any more. The transition-time call site (claim → in_process listener)
    # races the per-task pane bootstrap: the message can be sent into a
    # still-loading pane and silently lost while the stamp lands as if
    # delivery succeeded. The sweep handler is now the sole writer to the
    # marker — it only stamps after observing a successful send against
    # an actually-resolvable session, so a lost transition-time push gets
    # re-delivered on the next sweep tick. See ``handlers/sweep.py``.

    return {
        "outcome": "sent",
        "task_id": event.task_id,
        "session": target_name,
        "execution_version": execution_version,
    }


def _is_worker_kickoff_event(event: TaskAssignmentEvent) -> bool:
    """Return True when this event represents a worker-role kickoff.

    The kickoff_sent stamp (#922) is meaningful only for worker pings
    landing on per-task ``task-<project>-<N>`` panes. Reviewer / operator
    / agent pings already work via the long-lived shared sessions, so
    they don't need the force-push gate.
    """
    from pollypm.work.models import ActorType

    if event.actor_type is not ActorType.ROLE:
        return False
    if (event.actor_name or "").strip().lower() != "worker":
        return False
    # Review nodes are reviewer territory; the kickoff race only bites
    # the work / rework path on a freshly-spawned per-task pane.
    if event.current_node_kind == "review":
        return False
    return True


def _mark_kickoff_delivered(
    event: TaskAssignmentEvent,
    work_service: Any | None,
) -> None:
    """Best-effort: stamp the active execution row's kickoff_sent_at.

    #923: this is now called *only* from the sweep handler, after a
    confirmed-target send has succeeded. Stamping from the transition-
    time ``notify()`` was unsafe because the per-task pane is often
    still bootstrapping at claim time — the keystrokes land in a
    still-loading shell and are lost while the stamp records as if
    delivery succeeded.

    Silently no-ops when the work service doesn't expose
    ``mark_kickoff_sent`` (test doubles), when the event isn't a worker
    kickoff, or when the service raises — a missing stamp at worst lets
    the next sweep tick re-fire once.
    """
    if work_service is None:
        return
    if not _is_worker_kickoff_event(event):
        return
    marker = getattr(work_service, "mark_kickoff_sent", None)
    if not callable(marker):
        return
    try:
        marker(
            event.project,
            event.task_number,
            event.current_node,
            event.execution_version or None,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment_notify: mark_kickoff_sent failed for %s",
            event.task_id, exc_info=True,
        )


# ---------------------------------------------------------------------------
# Escalation helpers
# ---------------------------------------------------------------------------


def _alert_type_for(event: TaskAssignmentEvent) -> str:
    """Return the alert_type used to dedupe no-session alerts per task."""
    return f"no_session_for_assignment:{event.task_id}"


def _known_project_keys(services: Any | None) -> frozenset[str]:
    """Return the set of registered project keys, or an empty set.

    #1001: callers use this to short-circuit alert emission for a
    project that isn't (or no longer is) registered in the operator
    config. An empty set is intentionally ambiguous between "no
    config / test mode" and "explicit empty registry"; the sentinel
    ``None`` services object also collapses to empty so the legacy
    unrestricted behaviour is preserved when there's no registry to
    match against. Callers should only filter when the result is
    non-empty.
    """
    if services is None:
        return frozenset()
    keys: set[str] = set()
    for entry in getattr(services, "known_projects", ()) or ():
        key = getattr(entry, "key", None)
        if isinstance(key, str) and key:
            keys.add(key)
    return frozenset(keys)


def _project_is_registered(project: str, services: Any | None) -> bool:
    """Return True when ``project`` should still receive new alerts.

    Returns True when the registry is empty (no signal — preserve the
    legacy behaviour) or when ``project`` appears in the registry.
    Returns False only when there's an explicit non-empty registry and
    ``project`` is missing — that's the ghost-project case (#1001).
    """
    keys = _known_project_keys(services)
    if not keys:
        return True
    return project in keys


def clear_alerts_for_cancelled_task(
    *,
    task_id: str,
    project: str,
    role_names: tuple[str, ...] = ("worker",),
    has_other_active_for_role: dict[str, bool] | None = None,
    config_path: Path | None = None,
    store: Any | None = None,
) -> dict[str, Any]:
    """Clear no_session alerts that referenced a now-cancelled task.

    Two-tier cleanup matching the two alert families the sweep / notify
    path raises:

    * Per-task ``no_session_for_assignment:<task_id>`` — unambiguous.
      Always cleared, since the task is terminal.
    * Per-project ``(worker-<project>, no_session)`` — only cleared
      when no other active task on the project still routes to that
      role. ``has_other_active_for_role`` maps a role name to True
      when the project has another active task for that role; the
      caller computes this against the work service so we don't
      reach back across the work / plugins layering.

    Best-effort: any error opening services / clearing alerts is
    swallowed. The next sweep tick will re-emit if the task somehow
    re-enters an active state, and the existing #919 stale-alert
    sweep guard remains intact.

    ``store`` lets the caller inject the alert store directly — the
    work-service cancel path does this so we don't open a second
    SQLite connection against the same DB while the caller's
    transaction is still in flight. When ``store`` is ``None`` we
    resolve runtime services from config and use the resulting
    msg_store/state_store handle.
    """
    cleared_per_task = False
    cleared_project: list[str] = []
    owns_work_service = False
    services = None
    if store is None:
        services = load_runtime_services(config_path=config_path)
        owns_work_service = True
        store = services.msg_store or services.state_store
    if store is None:
        # Best-effort: nothing to do without a store. Still close every
        # incidental connection the resolver opened. #1374 — pre-fix
        # closed only services.work_service and leaked the fresh
        # ``StateStore`` opened by ``load_runtime_services``.
        if owns_work_service and services is not None:
            try:
                services.close()
            except Exception:  # noqa: BLE001
                logger.debug(
                    "task_assignment_notify clear_alerts: services.close raised",
                    exc_info=True,
                )
        return {
            "cleared_per_task": cleared_per_task,
            "cleared_project": cleared_project,
        }
    try:
        store.clear_alert(
            "task_assignment", f"no_session_for_assignment:{task_id}",
        )
        cleared_per_task = True
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment_notify: clear_alert(no_session_for_assignment) "
            "failed for %s", task_id, exc_info=True,
        )
    # Lazy import — keep this module's import graph independent of
    # ``pollypm.work.task_assignment`` for the simple alert-clear
    # path so test doubles can mock ``role_candidate_names``.
    from pollypm.work.task_assignment import role_candidate_names

    active_map = has_other_active_for_role or {}
    for role in role_names:
        # Project-level alert: skip if the project still has another
        # active task routed to this role. The per-task alert above is
        # unambiguous; the project-level one is a single row that must
        # remain visible while *any* task on that role is blocked.
        if active_map.get(role, False):
            continue
        for candidate in role_candidate_names(role, project):
            try:
                store.clear_alert(candidate, "no_session")
                cleared_project.append(candidate)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "task_assignment_notify: clear_alert(no_session) "
                    "failed for %s", candidate, exc_info=True,
                )
    # Close every connection we incidentally opened via
    # ``load_runtime_services`` — the cancel call site owns its own
    # connections and we mustn't leak ours. #1374 — pre-fix closed
    # only services.work_service and leaked the fresh StateStore
    # (regression of #1069's documented pattern).
    if owns_work_service and services is not None:
        try:
            services.close()
        except Exception:  # noqa: BLE001
            logger.debug(
                "task_assignment_notify clear_alerts: services.close raised",
                exc_info=True,
            )
    return {
        "cleared_per_task": cleared_per_task,
        "cleared_project": cleared_project,
    }


def clear_no_session_alert_for_task(
    *,
    task_id: str,
    config_path: Path | None = None,
    store: Any | None = None,
) -> dict[str, Any]:
    """Clear the per-task ``no_session_for_assignment:<task_id>`` alert.

    Narrower companion to :func:`clear_alerts_for_cancelled_task` for
    transitions where only the per-task alert is unambiguously stale,
    e.g. ``approve`` taking a task out of ``review`` (#953). The
    project-level ``worker-<project>/no_session`` alert is intentionally
    left alone — the task may still be active under a new role on the
    same project, and other active siblings may still need the role.

    Best-effort: any error opening services / clearing the alert is
    swallowed. The next sweep tick will re-emit if the task somehow
    re-enters an active state with no matching session.

    ``store`` lets the caller inject the alert store directly — the
    work-service approve path does this so we don't open a second
    SQLite connection against the same DB while the caller's
    transaction is still in flight. When ``store`` is ``None`` we
    resolve runtime services from config and use the resulting
    msg_store/state_store handle.
    """
    cleared_per_task = False
    owns_work_service = False
    services = None
    if store is None:
        services = load_runtime_services(config_path=config_path)
        owns_work_service = True
        store = services.msg_store or services.state_store
    try:
        if store is not None:
            try:
                store.clear_alert(
                    "task_assignment", f"no_session_for_assignment:{task_id}",
                )
                cleared_per_task = True
            except Exception:  # noqa: BLE001
                logger.debug(
                    "task_assignment_notify: clear_alert(no_session_for_assignment) "
                    "failed for %s", task_id, exc_info=True,
                )
    finally:
        # #1374 — close every owned sqlite connection. Pre-fix closed
        # only services.work_service and leaked the fresh StateStore
        # opened by load_runtime_services on every approve.
        if owns_work_service and services is not None:
            try:
                services.close()
            except Exception:  # noqa: BLE001
                logger.debug(
                    "task_assignment_notify clear_no_session_alert: "
                    "services.close raised",
                    exc_info=True,
                )
    return {"cleared_per_task": cleared_per_task}


def _escalate_no_session(
    event: TaskAssignmentEvent,
    store: Any | None,
    *,
    services: Any | None = None,
) -> None:
    """Raise (or refresh) a user-inbox alert when no session matches.

    #1001: when ``services`` is supplied and its ``known_projects``
    registry is non-empty, drop the escalation if ``event.project``
    isn't a registered project — the project was deregistered (or
    never existed) and the alert would be a ghost. Keeping the
    behaviour gated on a non-empty registry preserves legacy callers
    that pass no services (test doubles) or run without config.
    """
    if store is None:
        return
    if not _project_is_registered(event.project, services):
        return
    # #760 — action-forward single-line UI hint. Names the actor in plain
    # English and points at the cockpit recovery surface.
    # #953 — also append a ``Try:`` block with the CLI commands a CLI-only
    # operator can run. For reviewer-role alerts ``pm task approve`` is
    # listed first because CLI-driven approve is the canonical, documented
    # human-review path; for worker-role alerts ``pm task claim`` is first
    # because per-task workers are the default capacity model.
    from pollypm.work.models import ActorType

    cli_hint: str | None = None
    if event.actor_type is ActorType.ROLE:
        actor_display = f"the {event.actor_name} role"
        if event.actor_name == "architect":
            action_hint = (
                f"Open project '{event.project}' and use Workers to start or "
                "recover the architect."
            )
            cli_hint = (
                f"Try: pm worker-start --role architect {event.project}"
            )
        elif event.actor_name == "worker":
            action_hint = (
                "Open the task in Tasks; Polly will claim it when worker "
                "capacity is available, or use Workers to start capacity now."
            )
            # #1059 — drop the ``pm worker-start --role worker`` fallback;
            # that command is deprecated (per-task workers replaced the
            # managed-worker pattern). ``pm task claim`` is the only
            # supported per-task spawn path now.
            cli_hint = (
                f"Try: pm task claim {event.task_id}\n"
                f"     (a per-task worker auto-spawns on claim)"
            )
        elif event.actor_name == "reviewer":
            # #953 — human review is the canonical path; surface the
            # in-cockpit Approve/Reject decision before session recovery,
            # and lead the CLI hint with ``pm task approve``.
            action_hint = (
                "Open the task in Tasks or Inbox and use Approve or Reject."
            )
            cli_hint = (
                f"Try: pm task approve {event.task_id} --actor <reviewer> "
                "--reason \"...\"\n"
                f"     (or pm worker-start --role reviewer {event.project} "
                "for a long-running session)\n"
                f"     (or pm task claim {event.task_id} for a per-task worker)"
            )
        else:
            # #1057 — non-base roles (e.g. ``critic_simplicity``) don't
            # have a ``pm worker-start --role <X>`` path; they ship via
            # per-task workers (``task-<project>-<N>`` windows). The
            # role-assignment resolver should already accept that
            # window as fulfillment, so a no_session_for_assignment
            # alert against a non-base role usually means the per-task
            # worker isn't running yet (or has died).
            action_hint = (
                f"Open the task in Tasks to inspect the per-task worker "
                f"(``task-{event.project}-{event.task_number}``)."
            )
            cli_hint = (
                f"If the task is in progress (check pm task get "
                f"{event.task_id}), the per-task worker is fulfilling "
                f"the role and this alert is spurious — see #1057."
            )
    else:
        actor_display = event.actor_name or event.actor_type.value
        action_hint = (
            "Open the task in Tasks; Polly will claim it when a matching "
            "worker is available."
        )
    message = (
        f"Task {event.task_id} was routed to {actor_display} but no "
        f"matching session is running. {action_hint}"
    )
    if cli_hint:
        message = f"{message}\n{cli_hint}"
    try:
        store.upsert_alert(
            "task_assignment",
            _alert_type_for(event),
            "warning",
            message,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "task_assignment_notify: failed to raise alert for %s",
            event.task_id, exc_info=True,
        )


def _escalate_no_session_service(event: TaskAssignmentEvent, store: Any | None) -> None:
    """No session service at all — dev/test mode or misconfig. Surface once."""
    if store is None:
        return
    try:
        store.upsert_alert(
            "task_assignment",
            "no_session_service",
            "warning",
            (
                "Task-assignment notify cannot resolve a session service. "
                "Check plugin host configuration — pings will not be delivered "
                f"(example pending task: {event.task_id})."
            ),
        )
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Shared sweep utilities used by recurring jobs
# ---------------------------------------------------------------------------


def _record_sweeper_ping(
    work: Any,
    task_id: str,
    *,
    outcome: str,
    source: str,
) -> None:
    """Stamp ``task_id`` with a recent sweeper-ping marker."""
    if outcome not in {"sent", "deduped"}:
        return
    add_context = getattr(work, "add_context", None)
    if not callable(add_context):
        return
    try:
        add_context(
            task_id,
            "sweeper",
            f"{source}:{outcome}",
            entry_type=SWEEPER_PING_CONTEXT_ENTRY_TYPE,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment sweep: failed recording sweeper ping for %s",
            task_id,
            exc_info=True,
        )


def _auto_claim_enabled_for_project(services: Any, project: Any) -> bool:
    """Return True when this project is eligible for auto-claim."""
    if not getattr(services, "auto_claim", True):
        return False
    project_flag = getattr(project, "auto_claim", None)
    if project_flag is False:
        return False
    return True


def _tmux_window_alive_for_task(
    services: Any,
    project_key: str,
    task_number: int,
) -> bool:
    """Check whether the per-task tmux window for a claim is still alive."""
    from pollypm.work.session_manager import task_window_name

    session_service = getattr(services, "session_service", None)
    if session_service is None:
        return True
    expected_name = task_window_name(project_key, task_number)
    try:
        tmux = getattr(session_service, "tmux", None)
        if tmux is None:
            return True
        target_session = getattr(session_service, "storage_closet_session_name", None)
        if callable(target_session):
            session_name = target_session()
        else:
            session_name = "pollypm-storage-closet"
        windows = tmux.list_windows(session_name)
    except Exception:  # noqa: BLE001
        return True
    for window in windows or []:
        name = getattr(window, "name", "") or ""
        if name == expected_name and not getattr(window, "pane_dead", False):
            return True
    return False


def _consecutive_abandonments_at_active_node(task: Any) -> int:
    """Count consecutive abandoned executions at the task's current node."""
    current_node = getattr(task, "current_node_id", None)
    if not current_node:
        return 0
    executions = list(getattr(task, "executions", []) or [])
    try:
        executions.sort(
            key=lambda e: int(getattr(e, "visit", 0) or 0),
            reverse=True,
        )
    except Exception:  # noqa: BLE001
        return 0
    streak = 0
    abandoned_value = ExecutionStatus.ABANDONED.value
    for execution in executions:
        if getattr(execution, "node_id", None) != current_node:
            continue
        status = getattr(execution, "status", None)
        status_value = getattr(status, "value", status)
        if status_value == abandoned_value:
            streak += 1
            continue
        if status_value == ExecutionStatus.ACTIVE.value:
            continue
        break
    return streak


def _abandonments_within_window(
    task: Any,
    *,
    window_seconds: int = RATE_BREAKER_WINDOW_SECONDS,
    now: datetime | None = None,
) -> int:
    """Return the count of abandoned executions inside the rate window."""
    executions = list(getattr(task, "executions", []) or [])
    if not executions:
        return 0
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=window_seconds)
    abandoned_value = ExecutionStatus.ABANDONED.value
    count = 0
    for execution in executions:
        status = getattr(execution, "status", None)
        status_value = getattr(status, "value", status)
        if status_value != abandoned_value:
            continue
        ts_raw = (
            getattr(execution, "completed_at", None)
            or getattr(execution, "started_at", None)
        )
        if ts_raw is None:
            continue
        ts = _parse_execution_timestamp(ts_raw)
        if ts is not None and ts >= cutoff:
            count += 1
    return count


def _parse_execution_timestamp(value: Any) -> datetime | None:
    """Coerce an execution-row timestamp into a timezone-aware datetime."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str) and value:
        try:
            ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    return None


def _emit_spawn_failed_persistent_alert(
    services: Any,
    *,
    project: str,
    task_id: str,
    streak: int,
) -> None:
    """Escalate the project's worker alert after repeated spawn failures."""
    store = getattr(services, "msg_store", None) or getattr(
        services,
        "state_store",
        None,
    )
    if store is None:
        return
    candidates = role_candidate_names("worker", project)
    expected_name = candidates[0] if candidates else f"worker-{project}"
    message = (
        f"Auto-spawn for task {task_id} has failed {streak} times in a row "
        "— manual intervention required. The per-task worker session never "
        "materialised after the auto-claim sweep tried to spawn it. "
        f"Try `pm task claim {task_id}` from a clean shell, or check the "
        "supervisor logs for the underlying launch failure."
    )
    try:
        store.upsert_alert(
            expected_name,
            SPAWN_FAILED_PERSISTENT_ALERT_TYPE,
            "error",
            message,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_auto_claim: spawn_failed_persistent upsert_alert failed for %s",
            expected_name,
            exc_info=True,
        )


def _recover_dead_claims(
    services: Any,
    work: Any,
    project: Any,
    totals: dict[str, Any],
) -> None:
    """Unclaim active worker-role tasks whose tmux window is gone."""
    project_key = getattr(project, "key", None)
    if not project_key:
        return
    active_tasks: list[Any] = []
    for status in _ACTIVE_WORKER_STATUSES:
        try:
            active_tasks.extend(
                work.list_tasks(project=project_key, work_status=status)
            )
        except Exception:  # noqa: BLE001
            continue
    by_outcome = totals["by_outcome"]
    for task in active_tasks:
        roles = getattr(task, "roles", {}) or {}
        if "worker" not in roles:
            continue
        task_number = getattr(task, "task_number", None)
        if task_number is None:
            continue
        if _tmux_window_alive_for_task(services, project_key, task_number):
            continue

        task_id = getattr(task, "task_id", f"{project_key}/{task_number}")
        streak = _consecutive_abandonments_at_active_node(task)
        rate_count = _abandonments_within_window(task)
        streak_tripped = streak >= MAX_CONSECUTIVE_ABANDONMENTS
        rate_tripped = rate_count >= RATE_BREAKER_MAX_ABANDONMENTS
        if streak_tripped or rate_tripped:
            by_outcome["auto_claim_circuit_breaker"] = (
                by_outcome.get("auto_claim_circuit_breaker", 0) + 1
            )
            _emit_spawn_failed_persistent_alert(
                services,
                project=project_key,
                task_id=task_id,
                streak=max(streak, rate_count),
            )
            logger.warning(
                "task_auto_claim: circuit-breaker tripped for %s "
                "(streak=%d at node %r, rate=%d in last %ds); refusing "
                "to release stale claim until the underlying spawn "
                "failure is resolved (#1012/#1014)",
                task_id,
                streak,
                getattr(task, "current_node_id", None),
                rate_count,
                RATE_BREAKER_WINDOW_SECONDS,
            )
            continue

        try:
            release = getattr(work, "release_stale_claim", None)
            if not callable(release):
                raise RuntimeError(
                    "work service does not support release_stale_claim"
                )
            release(
                task_id,
                "auto_claim_sweep",
                reason="worker session missing",
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "task_auto_claim: stale claim release failed for %s",
                task_id,
                exc_info=True,
            )
            continue
        by_outcome["auto_claim_recovered"] = (
            by_outcome.get("auto_claim_recovered", 0) + 1
        )
        msg_store = getattr(services, "msg_store", None)
        if msg_store is not None:
            try:
                msg_store.append_event(
                    scope=project_key,
                    sender="auto_claim_sweep",
                    subject="worker_session_recovered",
                    payload={
                        "task_id": task_id,
                        "reason": "tmux window missing; task returned to queued",
                    },
                )
            except Exception:  # noqa: BLE001
                pass


def _wire_session_manager(svc: Any, project_root: Path, services: Any) -> None:
    """Best-effort session-manager wiring for a project-scoped service."""
    try:
        from pollypm.session_services import create_tmux_client
        from pollypm.work.session_manager import SessionManager

        if project_root.exists() and (project_root / ".git").exists():
            session_mgr = SessionManager(
                tmux_client=create_tmux_client(),
                work_service=svc,
                project_path=project_root,
                config=getattr(services, "config", None),
                session_service=getattr(services, "session_service", None),
                storage_closet_name=getattr(
                    services,
                    "storage_closet_name",
                    "pollypm-storage-closet",
                ),
            )
            svc.set_session_manager(session_mgr)
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment sweep: failed to wire session manager for %s",
            project_root,
            exc_info=True,
        )


def _open_project_work_service(project: Any, services: Any) -> Any | None:
    """Open a per-project ``SQLiteWorkService`` if its state DB exists."""
    project_path = getattr(project, "path", None)
    if project_path is None:
        return None
    db_path = Path(project_path) / ".pollypm" / "state.db"
    if not db_path.exists():
        return None
    try:
        from pollypm.work import create_work_service

        svc = create_work_service(
            db_path=db_path,
            project_path=Path(project_path),
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment sweep: failed to open per-project DB at %s",
            db_path,
            exc_info=True,
        )
        return None

    _wire_session_manager(svc, Path(project_path), services)
    return svc


def _close_quietly(svc: Any) -> None:
    closer = getattr(svc, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # noqa: BLE001
            pass


def auto_claim_enabled_for_project(*args: Any, **kwargs: Any) -> Any:
    return _auto_claim_enabled_for_project(*args, **kwargs)


def close_quietly(*args: Any, **kwargs: Any) -> Any:
    return _close_quietly(*args, **kwargs)


def open_project_work_service(*args: Any, **kwargs: Any) -> Any:
    return _open_project_work_service(*args, **kwargs)


def record_sweeper_ping(*args: Any, **kwargs: Any) -> Any:
    return _record_sweeper_ping(*args, **kwargs)


def recover_dead_claims(*args: Any, **kwargs: Any) -> Any:
    return _recover_dead_claims(*args, **kwargs)


__all__ = [
    "DEDUPE_WINDOW_SECONDS",
    "MAX_CONSECUTIVE_ABANDONMENTS",
    "RATE_BREAKER_MAX_ABANDONMENTS",
    "RATE_BREAKER_WINDOW_SECONDS",
    "RECENT_SWEEPER_PING_SECONDS",
    "SPAWN_FAILED_PERSISTENT_ALERT_TYPE",
    "SWEEPER_COOLDOWN_SECONDS",
    "SWEEPER_PING_CONTEXT_ENTRY_TYPE",
    "_RuntimeServices",
    "_abandonments_within_window",
    "_auto_claim_enabled_for_project",
    "_close_quietly",
    "_consecutive_abandonments_at_active_node",
    "_known_project_keys",
    "_mark_kickoff_delivered",
    "_open_project_work_service",
    "_parse_execution_timestamp",
    "_recover_dead_claims",
    "_tmux_window_alive_for_task",
    "auto_claim_enabled_for_project",
    "build_event_for_task",
    "clear_alerts_for_cancelled_task",
    "clear_no_session_alert_for_task",
    "close_quietly",
    "load_runtime_services",
    "notify",
    "open_project_work_service",
    "record_sweeper_ping",
    "recover_dead_claims",
]
