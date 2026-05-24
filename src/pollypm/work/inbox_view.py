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

from datetime import datetime, timezone
from typing import Iterable, Protocol

from pollypm.inbox.kind import InboxItemKind, coerce_kind
from pollypm.work.inbox_snooze import is_snooze_active
from pollypm.work.models import (
    ActorType,
    FlowTemplate,
    Priority,
    Task,
    TERMINAL_STATUSES,
    WorkStatus,
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
    Uses ``getattr`` so duck-typed test stubs without a ``roles``
    attribute degrade to "no user role" instead of ``AttributeError``
    — real ``Task`` instances always carry the field.
    """
    roles = getattr(task, "roles", None) or {}
    if "user" in roles:
        return True
    return any(value == "user" for value in roles.values())


def _current_node_is_human(
    task: Task, service: _FlowLookup, *, flow_cache: dict[tuple[str, int], FlowTemplate]
) -> bool:
    """True if the task's current flow node has actor_type == HUMAN.

    ``getattr`` on ``current_node_id`` so duck-typed test stubs that
    only carry ``flow_template_id`` + ``labels`` (the chat-flow /
    plan-review write tests) degrade to "no current node" cleanly.
    Real ``Task`` instances always carry the field.
    """
    if getattr(task, "current_node_id", None) is None:
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

    Exact-match check on ``plan_review`` (not substring) so labels
    like ``not_plan_review`` / ``planning`` cannot widen the inbox
    write surface by accident — Codex round-5 blocker on PR #2060.
    """
    labels = getattr(task, "labels", None) or []
    return any(label == "plan_review" for label in labels)


def _labels(task: Task) -> set[str]:
    return {str(label) for label in (getattr(task, "labels", None) or [])}


def inbox_item_type_for_task(task: Task) -> str:
    """Return the API inbox item type for a task-backed inbox row.

    The work row carries both legacy labels and the structured ``kind``
    discriminator. Keep the coarse API type derivation here so list filters,
    detail conversion, and non-HTTP consumers agree on the same taxonomy.
    """
    labels = _labels(task)
    if "blocking_question" in labels:
        return "blocking_question"

    kind = coerce_kind(getattr(task, "kind", None))
    if kind is InboxItemKind.PLAN_REVIEW_PENDING:
        return "plan_review"
    if kind is InboxItemKind.WATCHDOG_OPERATOR_DISPATCH:
        return "alert"
    if kind is not InboxItemKind.LEGACY:
        return kind.value

    if "plan_review" in labels:
        return "plan_review"
    return "message"


def inbox_task_matches_type(task: Task, type_filter: str | None) -> bool:
    """Return True when ``task`` matches an API ``type=`` filter."""
    if type_filter is None:
        return True
    wanted = str(type_filter).strip()
    if not wanted:
        return True
    if inbox_item_type_for_task(task) == wanted:
        return True
    kind = coerce_kind(getattr(task, "kind", None))
    return kind.value == wanted


def inbox_state_for_task(task: Task) -> str:
    """Map work-service lifecycle status to the API inbox state enum."""
    status = getattr(task, "work_status", None)
    value = getattr(status, "value", str(status)) if status else ""
    if status in TERMINAL_STATUSES or value in {"done", "cancelled"}:
        return "closed"
    if value == "review":
        return "waiting-on-pm"
    return "open"


def is_archived_inbox_task(
    task: Task,
    service: _FlowLookup,
    *,
    flow_cache: dict[tuple[str, int], FlowTemplate] | None = None,
) -> bool:
    """Return True iff ``task`` is a terminal row with inbox identity.

    The work data model has no separate ``closed`` inbox state; archived
    inbox rows are terminal work tasks (``done`` / ``cancelled``) that still
    carry the same non-status inbox identity. This predicate is the shared
    closed/resolved/archive check used by API state filters and archive
    conflict handling.
    """
    status = getattr(task, "work_status", None)
    value = getattr(status, "value", str(status)) if status else ""
    if status not in TERMINAL_STATUSES and value not in {"done", "cancelled"}:
        return False
    return is_inbox_task_identity(task, service, flow_cache=flow_cache)


def is_inbox_task_identity(
    task: Task,
    service: _FlowLookup,
    *,
    flow_cache: dict[tuple[str, int], FlowTemplate] | None = None,
) -> bool:
    """Return True if ``task`` has the non-status inbox identity.

    This intentionally ignores terminal status. Write endpoints use it
    only after the normal :func:`is_inbox_task` predicate rejects a
    terminal row, so they can distinguish "already archived inbox item"
    from "ordinary task that never belonged to the inbox" without
    duplicating membership rules.
    """
    if _roles_match_user(task):
        return True
    if _is_plan_review_label(task):
        return True
    cache = flow_cache if flow_cache is not None else {}
    return _current_node_is_human(task, service, flow_cache=cache)


def is_inbox_task(
    task: Task,
    service: _FlowLookup,
    *,
    flow_cache: dict[tuple[str, int], FlowTemplate] | None = None,
) -> bool:
    """Return True if ``task`` belongs in the user's inbox.

    Canonical predicate shared by the cockpit inbox panel, the
    dashboard inbox count, the rail badge, AND (since #2060 round-5)
    the API ``GET /inbox`` + write resolution helpers. Centralising
    here prevents write-side drift past the read surface: a chat-flow
    task lacking a user role / human current node cannot be archived,
    snoozed, mark-read, replied to, or promoted via the API if it
    would not have appeared in the cockpit inbox.
    """
    if getattr(task, "work_status", None) in TERMINAL_STATUSES:
        return False
    return is_inbox_task_identity(task, service, flow_cache=flow_cache)


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


def _filter_active_snoozes(
    service, tasks: list[Task], *, now: datetime | None = None,
) -> list[Task]:
    """Drop tasks whose latest snooze marker is still in the future.

    The HTTP ``GET /api/v1/inbox`` surface already filters snoozed
    items via :func:`pollypm.web_api.service._active_snoozed_ids` (uses
    :meth:`PgWorkService.latest_snoozes_bulk` + the shared
    :func:`pollypm.work.inbox_snooze.is_snooze_active` predicate). The
    cockpit / dashboard / rail path went through
    :func:`inbox_tasks` and never consulted snooze state, so a row
    snoozed via the API would vanish from ``/api/v1/inbox`` while
    still showing up in the cockpit inbox panel and dashboard count.
    Codex round-3 blocker #2 on #2060 — fix is to share the same
    membership + snooze predicate here so every surface agrees.

    Pg path uses the bulk SQL when available; degrades gracefully
    (no filter) when neither the bulk helper nor ``get_context`` is
    on the service. Callers using a mock ``_FakeService`` without
    snooze state see no behavioural change.
    """
    if not tasks:
        return tasks
    reference = now if now is not None else datetime.now(timezone.utc)
    bulk = getattr(service, "latest_snoozes_bulk", None)
    snoozed: set[str] = set()
    if callable(bulk):
        try:
            keys = [(t.project, t.task_number) for t in tasks]
            latest = bulk(keys)
        except Exception:  # noqa: BLE001 — readonly view degrades open
            latest = None
        if latest is not None:
            for task in tasks:
                entry = latest.get((task.project, task.task_number))
                if entry is None:
                    continue
                if is_snooze_active(getattr(entry, "text", "") or "", now=reference):
                    snoozed.add(task.task_id)
            if snoozed:
                return [t for t in tasks if t.task_id not in snoozed]
            return tasks
    # Fallback: per-task ``get_context`` loop. Kept for mock services
    # (test fakes) and backends without the bulk helper. Pg always has
    # the bulk helper so this branch should not run in production.
    get_context = getattr(service, "get_context", None)
    if not callable(get_context):
        return tasks
    for task in tasks:
        try:
            entries = get_context(task.task_id, entry_type="snooze", limit=1)
        except Exception:  # noqa: BLE001 — readonly view degrades open
            continue
        if not entries:
            continue
        text = getattr(entries[0], "text", "") or ""
        if is_snooze_active(text, now=reference):
            snoozed.add(task.task_id)
    if snoozed:
        return [t for t in tasks if t.task_id not in snoozed]
    return tasks


def inbox_tasks(
    service,
    *,
    project: str | None = None,
    now: datetime | None = None,
) -> list[Task]:
    """Return all inbox tasks, sorted user-review first within review rows.

    ``service`` must satisfy the WorkService protocol. In particular it must
    provide ``list_tasks(project=...)`` and ``get_flow(name, project=...)``.
    Snoozed rows (latest ``entry_type='snooze'`` context whose wake
    time is still in the future) are filtered out so the cockpit
    inbox panel agrees with ``GET /api/v1/inbox`` (#2060 round-3).
    """
    list_nonterminal = getattr(service, "list_nonterminal_tasks", None)
    if callable(list_nonterminal):
        candidates: Iterable[Task] = list_nonterminal(project=project)
    else:
        try:
            candidates = [
                task
                for status in WorkStatus
                if status not in TERMINAL_STATUSES
                for task in service.list_tasks(
                    project=project,
                    work_status=status.value,
                )
            ]
        except TypeError:
            candidates = service.list_tasks(project=project)
    flow_cache: dict[tuple[str, int], FlowTemplate] = {}
    matches = [
        task for task in candidates
        if is_inbox_task(task, service, flow_cache=flow_cache)
    ]
    matches = _filter_active_snoozes(service, matches, now=now)
    # Stable-sort twice so both keys descend: updated_at first (least
    # significant), priority second, then review owner split for rows in review.
    matches.sort(key=_updated_at_key, reverse=True)
    matches.sort(key=_priority_rank, reverse=True)
    matches.sort(
        key=lambda task: _review_owner_rank(task, service, flow_cache=flow_cache)
    )
    return matches
