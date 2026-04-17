"""``task_assignment.notify`` job handler.

Invoked with a JSON payload representing one
:class:`pollypm.work.task_assignment.TaskAssignmentEvent` — either
enqueued directly by the in-process event listener or (more commonly)
re-enqueued by the sweeper below.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from pollypm.work.models import ActorType
from pollypm.work.task_assignment import TaskAssignmentEvent

from pollypm.plugins_builtin.task_assignment_notify.resolver import (
    load_runtime_services,
    notify,
)

logger = logging.getLogger(__name__)


def _event_from_payload(payload: dict[str, Any]) -> TaskAssignmentEvent:
    """Rebuild a TaskAssignmentEvent from a serialized job payload."""
    actor_type_raw = payload.get("actor_type", "role")
    try:
        actor_type = ActorType(actor_type_raw)
    except (ValueError, KeyError):
        actor_type = ActorType.ROLE
    transitioned_at_raw = payload.get("transitioned_at")
    if transitioned_at_raw:
        try:
            transitioned_at = datetime.fromisoformat(transitioned_at_raw)
        except ValueError:
            transitioned_at = datetime.now()
    else:
        transitioned_at = datetime.now()
    return TaskAssignmentEvent(
        task_id=str(payload.get("task_id", "")),
        project=str(payload.get("project", "")),
        task_number=int(payload.get("task_number", 0) or 0),
        title=str(payload.get("title", "")),
        current_node=str(payload.get("current_node", "")),
        current_node_kind=str(payload.get("current_node_kind", "")),
        actor_type=actor_type,
        actor_name=str(payload.get("actor_name", "")),
        work_status=str(payload.get("work_status", "")),
        priority=str(payload.get("priority", "normal")),
        transitioned_at=transitioned_at,
        transitioned_by=str(payload.get("transitioned_by", "system")),
        commit_ref=payload.get("commit_ref"),
        execution_version=int(payload.get("execution_version", 0) or 0),
    )


def event_to_payload(event: TaskAssignmentEvent) -> dict[str, Any]:
    """Serialize an event for ``JobQueue.enqueue`` — JSON-friendly."""
    return {
        "task_id": event.task_id,
        "project": event.project,
        "task_number": event.task_number,
        "title": event.title,
        "current_node": event.current_node,
        "current_node_kind": event.current_node_kind,
        "actor_type": event.actor_type.value,
        "actor_name": event.actor_name,
        "work_status": event.work_status,
        "priority": event.priority,
        "transitioned_at": event.transitioned_at.isoformat(),
        "transitioned_by": event.transitioned_by,
        "commit_ref": event.commit_ref,
        # #279: carry the node's execution visit so the dedupe key on
        # the receiving side treats a reject-bounce as a fresh ping.
        "execution_version": int(event.execution_version or 0),
    }


def task_assignment_notify_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Send the ping (or escalate) for a single task-assignment event."""
    event = _event_from_payload(payload)
    config_path_hint = payload.get("config_path")
    config_path = Path(config_path_hint) if config_path_hint else None
    services = load_runtime_services(config_path=config_path)
    try:
        outcome = notify(event, services=services)
    finally:
        # Close the work service DB connection we opened (no-op if it
        # doesn't have a close()).
        closer = getattr(services.work_service, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass
    return outcome
