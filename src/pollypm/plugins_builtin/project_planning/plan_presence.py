"""Plan-presence gate — issue #273.

The sweeper refuses to delegate implementation tasks for a project
until that project has a non-trivial, user-approved plan on disk.
This module owns the predicate — ``has_acceptable_plan`` — and the
bypass check that lets the planner itself (plus explicit opt-outs)
skate past the gate.

Layout (locked in with the user):

    <project>/
      docs/
        plan/
          plan.md            <- canonical forward plan (gate reads this)
          milestones/        <- optional auxiliary files
          architecture.md    <- optional
          ...

The default plan directory is ``docs/plan``; it is overridable via
``[planner].plan_dir`` in ``pollypm.toml``. An absolute ``plan_dir``
is honoured as-is; a relative path resolves against the project root.

A plan is "acceptable" iff ALL of the following hold:

1. ``<project>/<plan_dir>/plan.md`` exists and has > 500 bytes of
   content after whitespace stripping. The byte threshold filters out
   empty scaffolding; it's not a quality bar, just a presence check.
2. At least one ``plan_project`` task exists for the project in the
   work-service with ``work_status='done'``.
3. That done task's ``user_approval`` flow-node execution carries a
   ``decision='approved'`` — a ``rejected`` decision disqualifies the
   plan even if the file looks complete.
4. ``plan.md``'s mtime is greater than or equal to the most recent
   non-planning task's ``created_at`` timestamp in the project. Projects
   with no non-planning tasks (fresh planner run, nothing emitted yet)
   auto-pass this check.

A separate helper — ``task_bypasses_plan_gate`` — returns True when a
task should skip the gate entirely. Two cases:

* The task is itself on the ``plan_project`` or ``critique_flow`` flow
  template. The planner can't be gated on its own output.
* The task carries the ``bypass_plan_gate`` label (explicit opt-out
  for migrations, hotfixes, operator bypass).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pollypm.work.models import Decision, ExecutionStatus, WorkStatus

logger = logging.getLogger(__name__)

# Minimum non-whitespace byte count before a ``plan.md`` counts as a
# real plan. Anything smaller is treated as scaffolding / placeholder.
MIN_PLAN_SIZE_BYTES = 500

# Label on a task that forces the gate to let it through. Checked
# case-sensitively against ``task.labels``.
BYPASS_LABEL = "bypass_plan_gate"

# Flow templates that produce the plan itself — these must never be
# gated on a plan existing, or the planner could never run.
_PLANNING_FLOWS = frozenset({"plan_project", "critique_flow"})

# Node name the plan_project flow uses for the single human approval
# touchpoint. If the flow shape ever changes, update this constant.
_APPROVAL_NODE = "user_approval"


def _resolve_plan_path(project_path: Path, plan_dir: str) -> Path:
    """Return the absolute path to ``plan.md`` for a project.

    ``plan_dir`` is the ``[planner].plan_dir`` config value (default
    ``"docs/plan"``). Absolute values win; relative values resolve
    against ``project_path``.
    """
    raw = Path(plan_dir)
    if raw.is_absolute():
        return raw / "plan.md"
    return project_path / raw / "plan.md"


def _plan_file_non_trivial(plan_path: Path) -> bool:
    """Read ``plan_path`` and return True when it has > 500 bytes of content.

    The read is intentionally tight — we compare the stripped length so
    a whitespace-only file is rejected. Any IO failure returns False so
    the gate fails closed. Callers should cache this decision per
    sweep-tick (see ``_PlanGateCache``) to avoid re-reading on every
    task.
    """
    try:
        if not plan_path.is_file():
            return False
        text = plan_path.read_text(encoding="utf-8")
    except OSError:
        return False
    return len(text.strip()) > MIN_PLAN_SIZE_BYTES


def _find_approved_plan_task(work_service: Any, project_key: str) -> Any | None:
    """Return the most recent done + approved ``plan_project`` task, or None.

    Iterates tasks in the project with ``work_status='done'`` and
    ``flow_template_id='plan_project'``. For each, checks that at least
    one ``user_approval`` node execution carries ``decision=APPROVED``.
    A ``REJECTED`` decision on the latest execution disqualifies the
    task — if the architect was later re-approved, a fresh done task
    should exist and will match. We return the first match; callers
    only need the boolean existence of one.
    """
    try:
        candidates = work_service.list_tasks(
            work_status=WorkStatus.DONE.value,
            project=project_key,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "plan_presence: list_tasks failed for project %s", project_key,
            exc_info=True,
        )
        return None
    for task in candidates:
        if task.flow_template_id != "plan_project":
            continue
        # Walk executions backwards — the latest decision on the
        # approval node is the binding one. ``reversed`` keeps us
        # O(n) with a tight early-exit.
        approved = False
        rejected = False
        for execution in reversed(task.executions):
            if execution.node_id != _APPROVAL_NODE:
                continue
            if execution.status is not ExecutionStatus.COMPLETED:
                continue
            if execution.decision is Decision.APPROVED:
                approved = True
                break
            if execution.decision is Decision.REJECTED:
                rejected = True
                break
        if approved and not rejected:
            return task
    return None


def _latest_backlog_created_at(work_service: Any, project_key: str) -> float | None:
    """Return the most recent ``created_at`` timestamp across non-planning tasks.

    Returns None when the project has no non-planning tasks (fresh
    project, only the plan_project task itself). The plan-staleness
    check auto-passes in that case — nothing to be stale relative to.

    Planning tasks (``flow_template_id in _PLANNING_FLOWS``) are
    excluded so the planner's own task doesn't count as "backlog" for
    the purpose of the staleness check.
    """
    try:
        tasks = work_service.list_tasks(project=project_key)
    except Exception:  # noqa: BLE001
        logger.debug(
            "plan_presence: list_tasks failed for project %s", project_key,
            exc_info=True,
        )
        return None
    latest: float | None = None
    for task in tasks:
        if task.flow_template_id in _PLANNING_FLOWS:
            continue
        if task.created_at is None:
            continue
        ts = task.created_at.timestamp()
        if latest is None or ts > latest:
            latest = ts
    return latest


def has_acceptable_plan(
    project_key: str,
    project_path: Path,
    work_service: Any,
    *,
    plan_dir: str = "docs/plan",
) -> bool:
    """Return True iff the project has a non-trivial, approved, fresh plan.

    See module docstring for the precise four-part contract. Fails
    closed on any error — if we can't read the plan file, we can't
    resolve the work service, or the plan mtime can't be stat'd, the
    gate denies.

    Callers should cache results per ``(project_key, sweep_tick)`` to
    avoid repeated disk reads across the many-queued-tasks case.
    """
    if work_service is None:
        return False
    plan_path = _resolve_plan_path(project_path, plan_dir)
    if not _plan_file_non_trivial(plan_path):
        return False
    if _find_approved_plan_task(work_service, project_key) is None:
        return False
    # Staleness check: plan.md must be at least as new as the newest
    # non-planning task. If there's no backlog yet, nothing to be
    # stale against.
    latest_backlog = _latest_backlog_created_at(work_service, project_key)
    if latest_backlog is not None:
        try:
            plan_mtime = plan_path.stat().st_mtime
        except OSError:
            return False
        if plan_mtime < latest_backlog:
            return False
    return True


def task_bypasses_plan_gate(task: Any) -> bool:
    """Return True if ``task`` is exempt from the plan-presence gate.

    Bypass cases:

    * ``task.flow_template_id`` is ``plan_project`` or ``critique_flow``
      — the planner produces the plan, so it can never be gated on its
      own output.
    * ``task.labels`` contains ``"bypass_plan_gate"`` — explicit
      operator opt-out for migrations / hotfixes.
    """
    flow_id = getattr(task, "flow_template_id", "") or ""
    if flow_id in _PLANNING_FLOWS:
        return True
    labels = getattr(task, "labels", None) or []
    return BYPASS_LABEL in labels
