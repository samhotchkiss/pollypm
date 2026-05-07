"""Project-level rail state rollup.

Contract:
- Inputs: task-like objects for one project plus optional plan-blocked
  and user-actionable alert task ids.
- Outputs: a small immutable rollup carrying the badge, sort severity,
  actionable rail key, and reason.
- Side effects: none.
- Invariants: this module does not open databases or route UI; callers
  own data loading and navigation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class ProjectRailState(str, Enum):
    RED = "red"
    YELLOW = "yellow"
    GREEN = "green"
    WORKING = "working"
    NONE = "none"


_BADGES: dict[ProjectRailState, str] = {
    ProjectRailState.RED: "🔴",
    ProjectRailState.YELLOW: "🟡",
    ProjectRailState.GREEN: "🟢",
    ProjectRailState.WORKING: "⚙️",
}

_SORT_RANKS: dict[ProjectRailState, int] = {
    ProjectRailState.RED: 0,
    ProjectRailState.YELLOW: 1,
    ProjectRailState.GREEN: 2,
    ProjectRailState.WORKING: 3,
    ProjectRailState.NONE: 4,
}

_TERMINAL_STATUSES = frozenset({"done", "accepted", "cancelled", "canceled"})
_INACTIVE_STATUSES = frozenset({"draft"})
_WAITING_STATUSES = frozenset({"waiting_on_user", "blocked", "on_hold"})
_AUTOMATED_STATUSES = frozenset({"queued", "in_progress"})
_PLAN_BYPASS_FLOWS = frozenset({"plan_project", "critique_flow"})
# Substring markers on ``current_node_id`` that indicate a human is
# the actor at this node. The downtime explore flow's parking node is
# ``awaiting_approval`` (actor_type: human, see
# plugins_builtin/downtime/flows/downtime_explore.yaml) — without
# ``approval`` here, a project where every task is parked at that
# node fell through to ``WORKING`` (working wheel) instead of
# ``GREEN`` (user review remaining), so the rail under-surfaced
# decisions waiting on the user.
_USER_NODE_MARKERS = ("human", "user", "approval")
_AUTOREVIEW_NODE_MARKERS = ("auto", "russell", "reviewer")

# Signal-bearing alert prefixes from #788. These must stay out of the
# operational/no-toast bucket: they represent task-level conditions where
# a user may need to intervene and therefore feed project rollups.
USER_ACTIONABLE_TASK_ALERT_PREFIXES: tuple[str, ...] = (
    "stuck_on_task:",
    "no_session_for_assignment:",
)


@dataclass(frozen=True, slots=True)
class ProjectStateRollup:
    state: ProjectRailState
    badge: str | None
    sort_rank: int
    actionable_key: str | None = None
    reason: str = ""
    # #1390 — Count of tasks where status=review AND a human is the
    # assignee/actor (e.g. user_approval node). The rail uses this to
    # render a prominent "needs your decision" affordance — savethenovel
    # had a plan parked at user_approval for hours without any
    # rail-level signal because the GREEN dot reads as "fine, moving".
    approvals_pending: int = 0


def task_id_for(task: object) -> str | None:
    """Return ``project/number`` for a task-like object when available."""
    task_id = getattr(task, "task_id", None)
    if isinstance(task_id, str) and task_id:
        return task_id
    project = getattr(task, "project", None)
    number = getattr(task, "task_number", None)
    if project is None or number is None:
        return None
    return f"{project}/{number}"


def actionable_alert_task_ids(
    alerts: Iterable[object],
    *,
    project_key: str,
) -> frozenset[str]:
    """Extract task ids for user-actionable project alerts."""
    prefix_by_name = USER_ACTIONABLE_TASK_ALERT_PREFIXES
    ids: set[str] = set()
    for alert in alerts:
        alert_type = str(getattr(alert, "alert_type", "") or "")
        for prefix in prefix_by_name:
            if not alert_type.startswith(prefix):
                continue
            task_id = alert_type[len(prefix):]
            if task_id.startswith(f"{project_key}/"):
                ids.add(task_id)
            break
    return frozenset(ids)


def rollup_project_state(
    project_key: str,
    tasks: Iterable[object],
    *,
    plan_blocked: bool = False,
    actionable_task_alert_ids: Iterable[str] = (),
) -> ProjectStateRollup:
    """Return the rail state for one project.

    The precedence is intentionally explicit: fully stopped user action
    first, partial user action next, final user sign-off next, automated
    progress next, and no badge only when there is no non-terminal work.
    """
    task_list = list(tasks)
    active_tasks = [
        task for task in task_list
        if not _is_terminal(task) and _status(task) not in _INACTIVE_STATUSES
    ]
    approvals_pending = sum(
        1 for task in active_tasks if _is_human_review(task)
    )
    if not active_tasks:
        return _rollup(ProjectRailState.NONE, project_key=project_key)

    alert_ids = frozenset(actionable_task_alert_ids)
    alerted_all = [
        task for task in active_tasks
        if task_id_for(task) in alert_ids
    ]
    user_waiting = [
        task for task in active_tasks
        if _is_waiting_on_user(task)
    ]
    # When a task is already in a user-waiting state, the alert that
    # fired on it (typically ``stuck_on_task:`` because the session
    # has been idle waiting for the user) is just the same fact in
    # different words — surfacing it as RED tells the user "something
    # is broken" when really the system is doing exactly what it
    # should: waiting on them. Drop those from the RED trigger so
    # the rail rollup matches the dashboard banner (yellow, not red).
    user_waiting_ids = {task_id_for(task) for task in user_waiting}
    alerted = [
        task for task in alerted_all
        if task_id_for(task) not in user_waiting_ids
    ]
    advanceable = [
        task for task in active_tasks
        if _can_independently_advance(task, plan_blocked=plan_blocked)
    ]

    # Red is reserved for true operational fault states — a worker
    # detected as stuck on a non-user-waiting task, a missing session
    # for an assignment, etc. A project where the user simply has
    # decisions to make is *not* a fault: it is "user attention
    # needed", which is yellow.
    if alerted:
        return _rollup(
            ProjectRailState.RED,
            project_key=project_key,
            task=_first_actionable_task(alerted),
            reason="operational alert needs review",
            approvals_pending=approvals_pending,
        )
    if user_waiting:
        return _rollup(
            ProjectRailState.YELLOW,
            project_key=project_key,
            task=_first_actionable_task(user_waiting),
            reason=(
                "all tasks waiting on user"
                if len(user_waiting) == len(active_tasks)
                else "some tasks waiting on user"
            ),
            approvals_pending=approvals_pending,
        )
    if all(_is_user_review(task) for task in active_tasks):
        return _rollup(
            ProjectRailState.GREEN,
            project_key=project_key,
            task=active_tasks[0],
            reason="user review remaining",
            approvals_pending=approvals_pending,
        )
    if any(_is_automated_progress(task) for task in active_tasks):
        return _rollup(
            ProjectRailState.WORKING,
            project_key=project_key,
            reason=(
                "plan needed before automated work"
                if plan_blocked and not advanceable
                else "automated work active"
            ),
            approvals_pending=approvals_pending,
        )
    return _rollup(
        ProjectRailState.WORKING,
        project_key=project_key,
        reason="non-terminal work remains",
        approvals_pending=approvals_pending,
    )


def _rollup(
    state: ProjectRailState,
    *,
    project_key: str,
    task: object | None = None,
    reason: str = "",
    approvals_pending: int = 0,
) -> ProjectStateRollup:
    return ProjectStateRollup(
        state=state,
        badge=_BADGES.get(state),
        sort_rank=_SORT_RANKS[state],
        actionable_key=_actionable_key(project_key, task),
        reason=reason,
        approvals_pending=approvals_pending,
    )


def _actionable_key(project_key: str, task: object | None) -> str | None:
    if task is None:
        return None
    return f"project:{project_key}:issues"


def _status(task: object) -> str:
    value = getattr(task, "work_status", getattr(task, "status", ""))
    return str(getattr(value, "value", value) or "")


def _node_id(task: object) -> str:
    return str(getattr(task, "current_node_id", "") or "").lower()


def _is_terminal(task: object) -> bool:
    return _status(task) in _TERMINAL_STATUSES


def _is_waiting_on_user(task: object) -> bool:
    return _status(task) in _WAITING_STATUSES


def _is_human_review(task: object) -> bool:
    """Return True for tasks parked at a human-decision review node.

    Scoped narrower than ``_is_user_review``: only counts tasks where
    ``status=='review'`` AND a human is named as the actor/assignee
    (or the node id explicitly marks a user/approval touchpoint).
    Drives the rail's ``(N approvals)`` affordance — see #1390.
    """
    if _status(task) != "review":
        return False
    owner = str(getattr(task, "owner", "") or "").lower()
    actor = str(getattr(task, "actor_type", "") or "").lower()
    assignee = str(getattr(task, "assignee", "") or "").lower()
    if owner in {"human", "user", "operator", "operator-pm"}:
        return True
    if actor == "human":
        return True
    if assignee == "human":
        return True
    node_id = _node_id(task)
    return any(marker in node_id for marker in _USER_NODE_MARKERS)


def _is_user_review(task: object) -> bool:
    status = _status(task)
    if status not in {"review", "user-review", "waiting_on_user"}:
        return False
    if status == "user-review":
        return True
    node_id = _node_id(task)
    owner = str(getattr(task, "owner", "") or "").lower()
    actor = str(getattr(task, "actor_type", "") or "").lower()
    if owner in {"human", "user", "operator", "operator-pm"}:
        return True
    if actor == "human":
        return True
    return any(marker in node_id for marker in _USER_NODE_MARKERS)


def _is_autoreview(task: object) -> bool:
    if _status(task) != "review":
        return False
    node_id = _node_id(task)
    if not node_id:
        return True
    return any(marker in node_id for marker in _AUTOREVIEW_NODE_MARKERS)


def _is_automated_progress(task: object) -> bool:
    status = _status(task)
    return status in _AUTOMATED_STATUSES or _is_autoreview(task)


def _bypasses_plan_gate(task: object) -> bool:
    flow_id = str(getattr(task, "flow_template_id", "") or "")
    if flow_id in _PLAN_BYPASS_FLOWS:
        return True
    labels = getattr(task, "labels", None) or []
    return "bypass_plan_gate" in labels


def _can_independently_advance(task: object, *, plan_blocked: bool) -> bool:
    if _is_terminal(task) or _is_waiting_on_user(task):
        return False
    if plan_blocked and not _bypasses_plan_gate(task):
        return False
    return _is_automated_progress(task)


def _first_actionable_task(tasks: Iterable[object]) -> object | None:
    return next(iter(tasks), None)
