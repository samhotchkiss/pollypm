"""Work-service-backed inbox view.

The "inbox" is the set of non-terminal tasks that the current human user is
expected to act on. It is *not* a separate storage subsystem — it is purely a
query over the work service.

A task is considered in the user's inbox when any of the following hold:
  * The task's current flow node has ``actor_type == human`` (the node is
    waiting on a human).
  * The task's ``roles`` dict contains a ``user`` key (the flow assigned a
    role named "user" to the task).
  * The task's ``roles`` dict contains *any* role whose value is literally
    the string ``"user"`` (a role like ``requester=user``).

Terminal tasks (``done`` / ``cancelled``) are always excluded.

Results are sorted by review owner first for review tasks (human review before
autoreview), then by priority descending, then by ``updated_at`` descending.
"""

from __future__ import annotations

from typing import Iterable, Protocol

from pollypm.work.models import (
    ActorType,
    FlowTemplate,
    Priority,
    Task,
    TERMINAL_STATUSES,
)


# ---------------------------------------------------------------------------
# Sort ordering
# ---------------------------------------------------------------------------


# Higher number = higher priority for sort key.
_PRIORITY_RANK: dict[Priority, int] = {
    Priority.CRITICAL: 4,
    Priority.HIGH: 3,
    Priority.NORMAL: 2,
    Priority.LOW: 1,
}


def _priority_rank(task: Task) -> int:
    return _PRIORITY_RANK.get(task.priority, 0)


def _updated_at_key(task: Task) -> str:
    """Return a string sort key for updated_at — empty string if missing."""
    if task.updated_at is None:
        return ""
    return task.updated_at.isoformat() if hasattr(task.updated_at, "isoformat") else str(task.updated_at)


def _status_value(task: Task) -> str:
    status = task.work_status
    return getattr(status, "value", None) or str(status or "")


def _flow_for_task(
    task: Task,
    service: _FlowLookup,
    *,
    flow_cache: dict[tuple[str, int], FlowTemplate],
) -> FlowTemplate | None:
    cache_key = (task.flow_template_id, task.flow_template_version)
    flow = flow_cache.get(cache_key)
    if flow is not None:
        return flow
    try:
        flow = service.get_flow(task.flow_template_id)
    except Exception:  # noqa: BLE001 - flow may be missing for legacy tasks
        return None
    flow_cache[cache_key] = flow
    return flow


def _review_owner_rank(
    task: Task,
    service: _FlowLookup,
    *,
    flow_cache: dict[tuple[str, int], FlowTemplate],
) -> int:
    """Return 0 for user-review rows and 1 for autoreview rows."""
    if _status_value(task) != "review" or task.current_node_id is None:
        return 0
    flow = _flow_for_task(task, service, flow_cache=flow_cache)
    if flow is None:
        return 1
    node = flow.nodes.get(task.current_node_id)
    if node is None:
        return 1
    if node.actor_type == ActorType.HUMAN:
        return 0
    if node.actor_type == ActorType.ROLE:
        owner = task.roles.get(node.actor_role or "", task.assignee)
        if owner == "human":
            return 0
    return 1


# ---------------------------------------------------------------------------
# Flow-lookup protocol
# ---------------------------------------------------------------------------


class _FlowLookup(Protocol):
    """Minimal protocol for resolving a flow template by (name, version)."""

    def get_flow(self, name: str, project: str | None = None) -> FlowTemplate:  # pragma: no cover - protocol
        ...


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------


def _roles_match_user(task: Task) -> bool:
    """True if the task has a 'user' role assignment.

    Matches both ``roles["user"] = <anything>`` and ``roles[<key>] = "user"``.
    """
    roles = task.roles or {}
    if "user" in roles:
        return True
    return any(value == "user" for value in roles.values())


def _current_node_is_human(
    task: Task, service: _FlowLookup, *, flow_cache: dict[tuple[str, int], FlowTemplate]
) -> bool:
    """True if the task's current flow node has actor_type == HUMAN."""
    if task.current_node_id is None:
        return False
    flow = _flow_for_task(task, service, flow_cache=flow_cache)
    if flow is None:
        return False
    node = flow.nodes.get(task.current_node_id)
    if node is None:
        return False
    return node.actor_type == ActorType.HUMAN


def _is_plan_review_label(task: Task) -> bool:
    """True when the task carries the ``plan_review`` label.

    Plan-review items (#297) may ship with ``requester=polly`` when
    fast-tracked, so the generic ``roles contains user`` membership
    test above would drop them. They still need to appear in the
    shared cockpit inbox (reviewed by Sam or by Polly on Sam's
    behalf), so we accept the label itself as a membership signal.
    """
    labels = task.labels or []
    return any(label == "plan_review" for label in labels)


def is_inbox_task(
    task: Task,
    service: _FlowLookup,
    *,
    flow_cache: dict[tuple[str, int], FlowTemplate] | None = None,
) -> bool:
    """Return True if ``task`` belongs in the user's inbox."""
    if task.work_status in TERMINAL_STATUSES:
        return False
    if _roles_match_user(task):
        return True
    if _is_plan_review_label(task):
        return True
    cache = flow_cache if flow_cache is not None else {}
    return _current_node_is_human(task, service, flow_cache=cache)


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


def inbox_tasks(
    service,
    *,
    project: str | None = None,
) -> list[Task]:
    """Return all inbox tasks, sorted user-review first within review rows.

    ``service`` must satisfy the WorkService protocol. In particular it must
    provide ``list_tasks(project=...)`` and ``get_flow(name, project=...)``.
    """
    candidates: Iterable[Task] = service.list_tasks(project=project)
    flow_cache: dict[tuple[str, int], FlowTemplate] = {}
    matches = [
        task for task in candidates
        if is_inbox_task(task, service, flow_cache=flow_cache)
    ]
    # Stable-sort twice so both keys descend: updated_at first (least
    # significant), priority second, then review owner split for rows in review.
    matches.sort(key=_updated_at_key, reverse=True)
    matches.sort(key=_priority_rank, reverse=True)
    matches.sort(
        key=lambda task: _review_owner_rank(task, service, flow_cache=flow_cache)
    )
    return matches
