"""``task_assignment.sweep`` job handler — the @every 30s fallback.

Catches assignment events that the in-process listener missed (daemon
restart mid-transition, sessions that booted after the original
transition, pre-existing state at plugin install).

Strategy: enumerate every task whose ``work_status`` is ``queued``,
``review``, or ``in_progress`` whose *current node* has
``actor_type != HUMAN``. For each, re-emit a ``TaskAssignmentEvent`` —
``notify()`` itself enforces the 30-minute throttle so this is cheap
to call frequently.

The ``in_progress`` branch (#246) is gated on session idleness — a
worker that's actively turning shouldn't be pinged mid-work. When the
target session is busy (active turn indicator visible in the pane),
we skip the ping and let the sweeper re-check on its next cadence.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pollypm.work.models import ActorType, WorkStatus
from pollypm.work.task_assignment import SessionRoleIndex, TaskAssignmentEvent

from pollypm.plugins_builtin.task_assignment_notify.resolver import (
    DEDUPE_WINDOW_SECONDS,
    SWEEPER_COOLDOWN_SECONDS,
    load_runtime_services,
    notify,
)

logger = logging.getLogger(__name__)

# Work statuses the sweeper cares about — those where a machine actor is
# the expected next mover. ``in_progress`` is gated on an idleness check
# (see ``_target_session_is_idle``) so we don't spam an actively-turning
# worker; ``queued`` and ``review`` are always safe to re-emit (dedupe
# handles throttling).
_SWEEPABLE_STATUSES = ("queued", "review", "in_progress")

# Statuses where an idle-session gate is required before notifying. The
# queued / review case is a new-or-pending assignment — pinging a busy
# session is fine because the ping just surfaces in their queue. The
# in_progress case means the worker claimed + started work, so only
# re-ping when they've gone idle (supervisor restart, Claude relaunched
# with no context, etc.).
_IDLE_GATED_STATUSES = frozenset({"in_progress"})


def _build_event_for_task(work_service: Any, task: Any) -> TaskAssignmentEvent | None:
    """Load the current flow node for ``task`` and return a synthetic event.

    Returns ``None`` when the task has no current node, the node doesn't
    exist in the flow, the node is HUMAN, or the node is terminal.

    For queued tasks without an explicit current node we fall back to
    the flow's ``start_node`` — that's the effective pickup node for
    the worker.
    """
    if not task.flow_template_id:
        return None
    try:
        flow = work_service._load_flow_from_db(
            task.flow_template_id, task.flow_template_version,
        )
    except Exception:  # noqa: BLE001
        return None
    node_id = task.current_node_id or flow.start_node
    if not node_id:
        return None
    node = flow.nodes.get(node_id)
    if node is None:
        return None
    actor_type = getattr(node, "actor_type", None)
    if actor_type is None or actor_type is ActorType.HUMAN:
        return None
    node_type = getattr(node, "type", None)
    node_kind = getattr(node_type, "value", node_type)
    if node_kind == "terminal":
        return None
    if actor_type is ActorType.AGENT:
        actor_name = getattr(node, "agent_name", "") or ""
    else:
        actor_name = getattr(node, "actor_role", "") or ""
    if not actor_name:
        return None
    priority = getattr(task.priority, "value", str(task.priority))
    return TaskAssignmentEvent(
        task_id=task.task_id,
        project=task.project,
        task_number=task.task_number,
        title=task.title,
        current_node=node_id,
        current_node_kind=str(node_kind) if node_kind is not None else "",
        actor_type=actor_type,
        actor_name=actor_name,
        work_status=task.work_status.value,
        priority=priority,
        transitioned_at=datetime.now(timezone.utc),
        transitioned_by="sweeper",
        commit_ref=None,
    )


def _target_session_is_idle(
    event: TaskAssignmentEvent,
    services: Any,
) -> bool:
    """Return True when the session the event would target is idle.

    "Idle" means the session service's ``is_turn_active(name)`` check
    returns False. If we can't resolve a session at all the function
    returns True — the notify path will still run and fall through to
    the ``no_session`` escalation, surfacing the problem to the user.

    Missing ``is_turn_active`` (exotic session services, test doubles)
    is treated as "idle" — the caller keeps the old behavior rather
    than silently dropping the ping.
    """
    session_svc = services.session_service
    if session_svc is None:
        # No session service → notify() will escalate. Let it run.
        return True
    try:
        index = SessionRoleIndex(session_svc, work_service=services.work_service)
        handle = index.resolve(event.actor_type, event.actor_name, event.project)
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment sweep: resolve failed for %s", event.task_id,
            exc_info=True,
        )
        return True
    if handle is None:
        # No match → notify() will escalate. Let it run.
        return True
    checker = getattr(session_svc, "is_turn_active", None)
    if not callable(checker):
        return True
    target = getattr(handle, "name", "")
    if not target:
        return True
    try:
        return not bool(checker(target))
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment sweep: is_turn_active(%s) failed", target,
            exc_info=True,
        )
        return True


def task_assignment_sweep_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Re-notify machine-actor tasks in queued/review/in_progress states."""
    config_path_hint = payload.get("config_path")
    config_path = Path(config_path_hint) if config_path_hint else None
    services = load_runtime_services(config_path=config_path)

    work = services.work_service
    if work is None:
        return {"outcome": "skipped", "reason": "no_work_service"}

    # The sweeper uses a shorter throttle so pre-existing queued tasks
    # get re-pinged every 5 min if they stay unclaimed — that's the
    # "session came online late" recovery path from the spec.
    throttle_override = int(payload.get("throttle_seconds", SWEEPER_COOLDOWN_SECONDS))
    if throttle_override < 1:
        throttle_override = SWEEPER_COOLDOWN_SECONDS

    total = 0
    by_outcome: dict[str, int] = {}
    try:
        for status in _SWEEPABLE_STATUSES:
            try:
                tasks = work.list_tasks(work_status=status)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "task_assignment sweep: list_tasks failed for %s", status,
                    exc_info=True,
                )
                continue
            for task in tasks:
                event = _build_event_for_task(work, task)
                if event is None:
                    continue
                # #246: for in_progress tasks, only ping if the worker
                # session is idle. An active turn means they're working;
                # resume pings are for the restart / crash-recovery case.
                if status in _IDLE_GATED_STATUSES:
                    if not _target_session_is_idle(event, services):
                        by_outcome["skipped_active_turn"] = (
                            by_outcome.get("skipped_active_turn", 0) + 1
                        )
                        continue
                total += 1
                result = notify(
                    event, services=services, throttle_seconds=throttle_override,
                )
                outcome = str(result.get("outcome", "unknown"))
                by_outcome[outcome] = by_outcome.get(outcome, 0) + 1
    finally:
        closer = getattr(work, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass

    return {
        "outcome": "swept",
        "considered": total,
        "by_outcome": by_outcome,
    }
