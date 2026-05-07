"""Task-assignment event types, role resolution helpers, and message formatters.

When a task transitions to a state whose current flow node has
``actor_type != user`` (i.e. a machine actor — role, agent, or
project_manager), the work service emits a :class:`TaskAssignmentEvent`
into this module's dispatch bus. The ``task_assignment_notify`` built-in
plugin subscribes here and turns events into ``SessionService.send()``
pings.

No ``SessionService`` protocol changes: role → session mapping is pure
naming convention, implemented by :class:`SessionRoleIndex`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from pollypm.work.models import ActorType, Priority

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event type
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class TaskAssignmentEvent:
    """Emitted by :meth:`SQLiteWorkService._sync_transition` whenever the
    new current node expects a non-user actor.

    * ``actor_type`` — the node's ``ActorType`` (role / agent /
      project_manager). Never ``HUMAN`` — those transitions go to a user
      inbox, not a session ping.
    * ``actor_name`` — the concrete role or agent name
      (e.g. ``"worker"``, ``"reviewer"``, ``"critic_simplicity"``).
    * ``current_node_kind`` — ``"work"`` / ``"review"`` / ``"terminal"``.
      Used by the formatter to pick between the "new work" and "review
      needed" ping variants.
    * ``work_status`` — the task's current work status (``"queued"`` /
      ``"in_progress"`` / ``"review"`` / ...). The sweeper uses this to
      re-enqueue only tasks sitting in ``queued`` or ``review``.
    * ``execution_version`` — the ``work_node_executions.visit`` counter
      for ``(task, current_node)`` at event-build time (#279). A
      reject-bounce that reopens an earlier node bumps the visit, which
      lets the notify dedupe treat the retry ping as a meaningfully-new
      event rather than suppressing it inside the 30-min window.
      Defaults to ``0`` for environments that can't compute a visit
      (bare test doubles, pre-#279 rehydrated events) — pre-migration
      dedupe semantics survive the default because existing
      ``task_notifications`` rows back-fill to ``0`` as well.
    """

    task_id: str
    project: str
    task_number: int
    title: str
    current_node: str
    current_node_kind: str
    actor_type: ActorType
    actor_name: str
    work_status: str
    priority: str
    transitioned_at: datetime
    transitioned_by: str
    commit_ref: str | None = None
    execution_version: int = 0


# ---------------------------------------------------------------------------
# Message formatters
# ---------------------------------------------------------------------------


def format_ping_for_role(event: TaskAssignmentEvent) -> str:
    """Render the short imperative ping sent into the target session.

    The ping bifurcates on the current node's kind: a ``review`` node
    means the task is sitting in REVIEW waiting on a reviewer, a
    ``work`` node means fresh / resumed work is queued.
    """
    # Reviewer-style ping: the task is parked at a review node.
    if event.current_node_kind == "review":
        commit_tag = f" (committed {event.commit_ref})" if event.commit_ref else ""
        return (
            f"\U0001F4CB Review needed: [{event.task_id}] {event.title}{commit_tag}\n"
            f"Run: pm task get {event.task_id}\n"
            f"Then: pm task approve {event.task_id} "
            f"OR pm task reject {event.task_id} --reason \"...\""
        )
    # Worker-style ping: work is ready to claim (or resume).
    # When the task is already claimed (in_progress after reject/resume)
    # the run-line still works — ``pm task claim`` is a no-op on a
    # self-claimed task and the pane text carries the pointer either way.
    if event.work_status == "queued":
        return (
            f"\U0001F4CB New work: [{event.task_id}] {event.title}\n"
            f"Run: pm task claim {event.task_id}"
        )
    return (
        f"\U0001F4CB Resume work: [{event.task_id}] {event.title}\n"
        f"Current node: {event.current_node}\n"
        f"Run: pm task get {event.task_id}"
    )


# ---------------------------------------------------------------------------
# Role → candidate session-name resolver
# ---------------------------------------------------------------------------


# Role → session naming conventions. ``role:worker`` and
# ``role:architect`` are project-scoped (one session per project); the
# other roles listed here are process-wide singletons.
_ROLE_STATIC_NAMES: dict[str, tuple[str, ...]] = {
    # Each role lists both the session-config key (what ``sessions`` table
    # stores as ``name``) and the window name (legacy convention). #272.
    "reviewer": ("reviewer", "pm-reviewer"),
    "operator": ("operator", "pm-operator"),
    "heartbeat-supervisor": ("heartbeat", "pm-heartbeat"),
    "heartbeat": ("heartbeat", "pm-heartbeat"),  # common alias
    "triage": ("triage", "pm-triage"),
}


# Project-scoped roles expand to both hyphen- and underscore-separated
# session names (``<role>-<project>`` and ``<role>_<project>``). The
# worker shipping convention is underscore (``workers.py``), but the
# resolver is tolerant of either so naming drift between operators,
# tests, and docs doesn't break lookup.
_PROJECT_SCOPED_ROLES: frozenset[str] = frozenset({"worker", "architect"})


def role_candidate_names(
    role: str,
    project: str,
    *,
    task_number: int | None = None,
) -> list[str]:
    """Return the ordered list of session names a role *could* map to.

    Callers match the first candidate that actually has a live session.
    Project-scoped roles (``worker``, ``architect``) expand to both
    ``<role>-<project>`` and ``<role>_<project>`` — we accept either
    convention so drift between docs and shipping code doesn't break
    lookup. Process-wide singletons (reviewer / operator / heartbeat /
    triage) map to their fixed session names via ``_ROLE_STATIC_NAMES``,
    BUT when a non-empty ``project`` is supplied we also include the
    per-project session form (``<role>_<project>``, ``<role>-<project>``)
    *before* the singleton fallback. ``pm worker-start --role reviewer
    <project>`` (and #1005's auto-recovery dispatch that mirrors it)
    creates a per-project ``reviewer_<project>`` session; without the
    per-project candidate the resolver only sees the singleton form and
    keeps reporting ``no_session`` even after the spawn lands. The
    singleton candidates stay in the list so workspaces that still run
    a singleton ``reviewer``/``operator`` session keep resolving via
    the legacy fallback. (#1011)

    When ``task_number`` is supplied for the ``worker`` role (#921), the
    canonical per-task window name (``task-<project>-<N>``, written by
    :func:`pollypm.work.session_manager.task_window_name`) is prepended
    to the candidate list. Per-task workers are the post-#919 shipping
    convention; the legacy long-lived ``worker-<project>`` /
    ``worker_<project>`` sessions remain accepted as fallbacks so a
    project that still runs a long-lived worker (e.g. booktalk's pre-
    existing setup) keeps working.

    #1057 — the per-task window also fulfills NON-worker roles for that
    specific task. When the planner spawns critic subtasks the task is
    in_progress with assignee ``critic_simplicity`` (or similar planner-
    emitted role) and a ``task-<project>-<N>`` window is doing the work.
    The role-assignment resolver must accept that window as fulfillment
    so the ``no_session_for_assignment`` alert doesn't fire spuriously.
    The shape: when ``task_number`` is supplied for *any* role, prepend
    ``task-<project>-<N>`` ahead of the role-specific candidates. The
    long-lived ``<role>-<project>`` / ``<role>_<project>`` candidates
    stay so workspaces that still run dedicated role sessions keep
    resolving via the legacy fallback.
    """
    key = role.strip().lower()
    # #1057 — per-task worker windows fulfill ANY role for their
    # specific task. Compute the per-task candidate once and prepend
    # below so it takes precedence over role-specific candidates.
    # Note: the simpler "per-task worker fulfills any role" semantics
    # is the shipped behavior (#1057 CLOSED). If we ever decide a
    # per-task pane should only fulfill the role it was spawned to
    # handle (e.g. because the window's persona was overwritten),
    # narrow this check by reading the task's ``assignee`` and matching
    # it against ``role``.
    per_task_candidate: str | None = None
    if task_number is not None and project:
        per_task_candidate = f"task-{project}-{int(task_number)}"
    if key in _PROJECT_SCOPED_ROLES:
        names: list[str] = []
        if per_task_candidate is not None:
            # Per-task worker sessions take priority — that's the
            # post-#919 canonical form. ``task-<project>-<N>`` mirrors
            # :func:`pollypm.work.session_manager.task_window_name`.
            names.append(per_task_candidate)
        names.extend([f"{key}-{project}", f"{key}_{project}"])
        return names
    if key.startswith("critic_") or key.startswith("critic-"):
        # Planner-spawned per-task critic sessions carry the role name
        # verbatim. Pass through so exact-name lookup picks them up.
        # #1057 — also accept the per-task window when ``task_number``
        # is supplied: critic subtasks run on the canonical ``task-<project>-<N>``
        # pane, not a session named after the persona.
        if per_task_candidate is not None:
            return [per_task_candidate, role]
        return [role]
    static = list(_ROLE_STATIC_NAMES.get(key, ()))
    if not static:
        # No static singleton mapping. If we have a per-task candidate
        # (e.g. an unknown role on a specific task) still return it so
        # the resolver can find the per-task worker.
        if per_task_candidate is not None:
            return [per_task_candidate]
        return static
    # #1011 — singleton-named control roles (reviewer/operator/triage/
    # heartbeat) can ALSO ship as per-project sessions when the auto-
    # recovery sweep dispatches ``pm worker-start --role <role>
    # <project>``. Prepend the per-project candidates so the resolver
    # finds the spawned session; keep the singleton form as a fallback
    # for workspaces that still run a singleton.
    project_key = (project or "").strip()
    if not project_key:
        return static
    project_scoped = [f"{key}_{project_key}", f"{key}-{project_key}"]
    # #1057 — per-task window also fulfills singleton-named control
    # roles (reviewer/operator/triage/heartbeat) for that task.
    if per_task_candidate is not None:
        return [per_task_candidate, *project_scoped, *static]
    return project_scoped + static


def agent_candidate_names(agent_name: str) -> list[str]:
    """Agent-pinned nodes look up sessions by exact name."""
    return [agent_name]


# ---------------------------------------------------------------------------
# SessionRoleIndex
# ---------------------------------------------------------------------------


class SessionRoleIndex:
    """Resolve ``actor_type:actor_name`` tuples to a concrete session.

    Scans the session service's live session list (``session_service.list()``)
    and filters by naming convention. No mutation, no caching — callers
    create one per resolve batch.

    Disambiguation when multiple sessions match:

    1. Least-busy (fewest in-progress claims against the work DB). When
       the ``work_service`` argument is ``None`` we skip this step.
    2. Tie-break by most recent activity. We proxy "most recent" by the
       session's ``started_at`` if exposed, otherwise by original list
       order (stable).
    """

    def __init__(
        self,
        session_service: object,
        *,
        work_service: object | None = None,
    ) -> None:
        self._svc = session_service
        self._work = work_service
        # Live snapshot of session handles, keyed by name.
        self._by_name: dict[str, object] = {}
        try:
            for handle in self._svc.list():
                self._by_name[handle.name] = handle
        except Exception:  # noqa: BLE001
            logger.debug("SessionRoleIndex: session_service.list() failed", exc_info=True)
            self._by_name = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(
        self,
        actor_type: ActorType,
        actor_name: str,
        project: str,
        *,
        task_number: int | None = None,
    ) -> object | None:
        """Return a ``SessionHandle`` for the expected actor, or ``None``
        if no live session matches.

        ``task_number`` (#921) — when supplied for a worker-role lookup,
        ``role_candidate_names`` prepends the canonical per-task window
        name ``task-<project>-<N>`` so the post-#919 per-task worker
        session counts as fulfilling the worker role for that task.
        """
        if actor_type is ActorType.HUMAN:
            # Humans don't have sessions — caller escalates.
            return None

        if actor_type is ActorType.AGENT:
            candidates = agent_candidate_names(actor_name)
        else:
            # ROLE / PROJECT_MANAGER — PROJECT_MANAGER is currently
            # treated as role:operator per the spec's list.
            effective = actor_name
            if actor_type is ActorType.PROJECT_MANAGER and not effective:
                effective = "operator"
            candidates = role_candidate_names(
                effective, project, task_number=task_number,
            )

        matches: list[object] = []
        for name in candidates:
            handle = self._by_name.get(name)
            if handle is not None:
                matches.append(handle)
        if not matches:
            return None
        if len(matches) == 1:
            return matches[0]
        return self._pick_least_busy(matches)

    # ------------------------------------------------------------------
    # Disambiguation
    # ------------------------------------------------------------------

    def _pick_least_busy(self, matches: list[object]) -> object:
        """Prefer the match with the fewest in-progress claims.

        Falls back to input order on ties or when the work service isn't
        wired — which keeps dashes-first / underscores-second precedence
        for ``worker-<project>`` vs ``worker_<project>``.
        """
        work = self._work
        if work is None:
            return matches[0]
        scored: list[tuple[int, int, object]] = []
        for i, handle in enumerate(matches):
            name = getattr(handle, "name", "")
            try:
                claims = self._in_progress_count_for_assignee(name)
            except Exception:  # noqa: BLE001
                claims = 0
            scored.append((claims, i, handle))
        scored.sort(key=lambda t: (t[0], t[1]))
        return scored[0][2]

    def _in_progress_count_for_assignee(self, assignee: str) -> int:
        """Count tasks currently claimed by ``assignee``.

        Uses the work service's ``list`` + ``Task.assignee`` — intentionally
        tolerant: any exception short-circuits to 0 so resolution never
        fails because of a busy-detection accounting miss.
        """
        work = self._work
        if work is None or not assignee:
            return 0
        lister = getattr(work, "list_tasks", None)
        if not callable(lister):
            # Fall back to a plain ``list`` method for alternative work
            # service implementations (mocks / future backends).
            lister = getattr(work, "list", None)
            if not callable(lister):
                return 0
        try:
            tasks = lister(work_status="in_progress", assignee=assignee)
        except TypeError:
            try:
                tasks = lister()
            except Exception:  # noqa: BLE001
                return 0
        except Exception:  # noqa: BLE001
            return 0
        count = 0
        for task in tasks:
            status = getattr(task, "work_status", None)
            value = getattr(status, "value", status)
            if value != "in_progress":
                continue
            if getattr(task, "assignee", None) == assignee:
                count += 1
        return count


# ---------------------------------------------------------------------------
# Dispatch bus — plugin subscribers register here at initialize() time
# ---------------------------------------------------------------------------


TaskAssignmentListener = Callable[[TaskAssignmentEvent], None]

_listeners: list[TaskAssignmentListener] = []


def register_listener(listener: TaskAssignmentListener) -> None:
    """Register ``listener`` as a subscriber of task-assignment events.

    Idempotent — the same callable is never registered twice. Plugins
    call this from their ``initialize(api)`` hook.
    """
    if listener not in _listeners:
        _listeners.append(listener)


def unregister_listener(listener: TaskAssignmentListener) -> None:
    """Remove a previously-registered listener. No-op if not present.

    Exposed for tests that want a clean slate between assertions.
    """
    try:
        _listeners.remove(listener)
    except ValueError:
        pass


def clear_listeners() -> None:
    """Drop every registered listener — test-only helper."""
    _listeners.clear()


def dispatch(event: TaskAssignmentEvent) -> None:
    """Deliver ``event`` to every registered subscriber.

    Subscribers are called in registration order. Exceptions from one
    subscriber are logged and swallowed so a misbehaving plugin can't
    break the work service's transition path.
    """
    for listener in list(_listeners):
        try:
            listener(event)
        except Exception:  # noqa: BLE001
            logger.exception(
                "task_assignment listener %r raised on %s",
                getattr(listener, "__name__", listener), event.task_id,
            )


# ---------------------------------------------------------------------------
# Construction helper used by SQLiteWorkService._sync_transition
# ---------------------------------------------------------------------------


def build_event_from_task(
    task: object,
    node: object,
    *,
    transitioned_by: str,
    commit_ref: str | None = None,
    execution_version: int = 0,
) -> TaskAssignmentEvent | None:
    """Build a :class:`TaskAssignmentEvent` from a task + its current node.

    Returns ``None`` if the node has no machine-actor binding (i.e. the
    node is a human-review node, a terminal node, or lacks the
    ``actor_type`` attribute).

    ``execution_version`` (#279) should be the ``visit`` counter on the
    current node's active execution row. Pass ``0`` when the work
    service can't resolve it — the dedupe still behaves correctly for
    pre-migration rows, and the notify path will simply treat
    ``visit=0`` as one identity bucket.
    """
    actor_type = getattr(node, "actor_type", None)
    if actor_type is None:
        return None
    if actor_type is ActorType.HUMAN:
        return None
    # Terminal nodes carry no onward work — nobody to notify.
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

    priority = getattr(task, "priority", Priority.NORMAL)
    priority_str = getattr(priority, "value", str(priority))
    status = getattr(task, "work_status", None)
    status_str = getattr(status, "value", str(status))
    node_kind_str = str(node_kind) if node_kind is not None else ""

    return TaskAssignmentEvent(
        task_id=getattr(task, "task_id", ""),
        project=getattr(task, "project", ""),
        task_number=int(getattr(task, "task_number", 0) or 0),
        title=getattr(task, "title", ""),
        current_node=getattr(task, "current_node_id", "") or "",
        current_node_kind=node_kind_str,
        actor_type=actor_type,
        actor_name=actor_name,
        work_status=status_str,
        priority=priority_str,
        transitioned_at=datetime.now(timezone.utc),
        transitioned_by=transitioned_by,
        commit_ref=commit_ref,
        execution_version=int(execution_version or 0),
    )
