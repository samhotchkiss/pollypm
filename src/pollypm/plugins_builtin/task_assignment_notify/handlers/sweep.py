"""``task_assignment.sweep`` job handler — the @every 30s fallback.

Catches assignment events that the in-process listener missed (daemon
restart mid-transition, sessions that booted after the original
transition, pre-existing state at plugin install).

Strategy: enumerate every task whose ``work_status`` is ``queued`` or
``review`` whose *current node* has ``actor_type != HUMAN``. For each,
re-emit a ``TaskAssignmentEvent`` — ``notify()`` itself enforces the
30-minute throttle so this is cheap to call frequently.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pollypm.work.models import ActorType, WorkStatus
from pollypm.work.task_assignment import TaskAssignmentEvent

from pollypm.plugins_builtin.task_assignment_notify.resolver import (
    DEDUPE_WINDOW_SECONDS,
    SWEEPER_COOLDOWN_SECONDS,
    load_runtime_services,
    notify,
)

logger = logging.getLogger(__name__)

# Work statuses the sweeper cares about — those where a machine actor is
# the expected next mover.
_SWEEPABLE_STATUSES = ("queued", "review")


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


def task_assignment_sweep_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Re-notify machine-actor tasks in ``queued`` / ``review`` states."""
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
