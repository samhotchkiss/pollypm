"""Plan-presence gate — issues #273, #281.

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
4. The plan's approval timestamp is greater than or equal to the most
   recent non-planning task's ``created_at`` timestamp in the project.
   Projects with no non-planning tasks auto-pass this staleness check.
   (#281) The approval timestamp comes from a ``plan_approved`` context
   entry written when the user approves the user_approval node. For
   plans approved before this change shipped, the timestamp falls back
   to the ``user_approval`` execution's ``completed_at``. File mtime
   is **not** used — git operations, editor saves, and the planner's
   own stage-8 emit all perturb it, making the gate unstable.

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

# Inbox / artifact tasks live on the chat flow and are not "project
# backlog" for plan-staleness purposes. Counting them would let a
# rejection-feedback inbox item (created after review) make an already-
# approved plan look stale and freeze unrelated worker delegation.
_NON_BACKLOG_FLOWS = frozenset({"chat"})

# Labels used by structured feedback/inbox artifacts that should never
# invalidate the project plan. Kept local so the gate does not need to
# import the higher-level rejection-feedback module.
_NON_BACKLOG_LABELS = frozenset({"review_feedback"})

# Node name the plan_project flow uses for the single human approval
# touchpoint. If the flow shape ever changes, update this constant.
_APPROVAL_NODE = "user_approval"


# Canonical relative paths to a project's plan file, in preference
# order. ``docs/plan/plan.md`` is the original spec path; the
# architect's approval helper (pollypm.plugins_builtin.project_planning.approval)
# writes ``docs/project-plan.md`` instead. Both are acceptable plans.
# Every code path that reads "the plan" — the presence gate, the
# advisor, the recovery reconciler, the cockpit plan-review view —
# should scan this tuple rather than inline its own copy. #768.
CANONICAL_PLAN_RELATIVE_PATHS: tuple[str, ...] = (
    "docs/plan/plan.md",
    "docs/project-plan.md",
)


def _candidate_plan_paths(project_path: Path, plan_dir: str) -> list[Path]:
    """Return the plan-file paths the gate accepts, in precedence order.

    The architect's approval helper writes ``docs/project-plan.md``
    (see :mod:`pollypm.plugins_builtin.project_planning.approval`), but
    the original gate spec named ``docs/plan/plan.md``. Both forms
    appear in real projects tonight — the Notesy repo has the former
    but not the latter, so the gate was (silently) refusing to
    delegate tasks for a fully-approved project. #765 / Notesy root-
    cause: accept either canonical path. Mirrors the same list in
    :mod:`pollypm.recovery.state_reconciliation`.

    ``plan_dir`` is the ``[planner].plan_dir`` config value (default
    ``"docs/plan"``). Absolute values win; relative values resolve
    against ``project_path``.
    """
    raw = Path(plan_dir)
    if raw.is_absolute():
        primary = raw / "plan.md"
        # Absolute plan_dir overrides conventional defaults — the user
        # picked this path explicitly, so we don't also look at the
        # project-relative default.
        return [primary]
    primary = project_path / raw / "plan.md"
    paths: list[Path] = [primary]
    for relative in CANONICAL_PLAN_RELATIVE_PATHS:
        candidate = project_path / relative
        if candidate not in paths:
            paths.append(candidate)
    return paths


def _resolve_plan_path(project_path: Path, plan_dir: str) -> Path:
    """Return the first-choice plan path for a project (back-compat).

    Kept for callers that want the canonical/primary path. New code
    should prefer :func:`_first_non_trivial_plan_path`, which honours
    the fallback to ``docs/project-plan.md``.
    """
    return _candidate_plan_paths(project_path, plan_dir)[0]


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


def _first_non_trivial_plan_path(
    project_path: Path, plan_dir: str,
) -> Path | None:
    """Return the first candidate plan path that passes ``_plan_file_non_trivial``.

    Returns ``None`` if no candidate passes — the gate fails closed at
    the call site.
    """
    for candidate in _candidate_plan_paths(project_path, plan_dir):
        if _plan_file_non_trivial(candidate):
            return candidate
    return None


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


def _plan_approved_at(work_service: Any, plan_task: Any) -> float | None:
    """Return the approval timestamp (epoch seconds) for ``plan_task``.

    Preferred source: a ``work_context_entries`` row with
    ``entry_type='plan_approved'`` — written inside the work-service
    ``approve()`` call when a ``plan_project`` task's ``user_approval``
    node is approved (issue #281).

    Fallback for plans approved before #281 shipped: derive the
    timestamp from the ``user_approval`` execution's ``completed_at``.
    The fallback is critical — without it, every project that planned
    pre-fix would be stuck behind the gate forever.

    Returns None only when the plan is malformed (approved but no
    completion timestamp anywhere) — which is vanishingly rare and
    fails closed on the staleness check.
    """
    task_id = getattr(plan_task, "task_id", None)
    if task_id:
        try:
            entries = work_service.get_context(
                task_id, entry_type="plan_approved",
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "plan_presence: get_context failed for %s", task_id,
                exc_info=True,
            )
            entries = []
        if entries:
            # get_context returns newest-first; take the most recent.
            ts = entries[0].timestamp
            if ts is not None:
                return ts.timestamp()

    # Fallback: the user_approval execution's completed_at. Walk
    # backwards to find the latest APPROVED execution, matching the
    # scan in _find_approved_plan_task.
    for execution in reversed(getattr(plan_task, "executions", []) or []):
        if execution.node_id != _APPROVAL_NODE:
            continue
        if execution.status is not ExecutionStatus.COMPLETED:
            continue
        if execution.decision is Decision.APPROVED:
            completed = execution.completed_at
            if completed is not None:
                return completed.timestamp()
            return None
    return None


def _latest_backlog_created_at(work_service: Any, project_key: str) -> float | None:
    """Return the most recent ``created_at`` timestamp across non-planning tasks.

    Returns None when the project has no non-planning tasks (fresh
    project, only the plan_project task itself). The plan-staleness
    check auto-passes in that case — nothing to be stale relative to.

    Planning tasks (``flow_template_id in _PLANNING_FLOWS``) are
    excluded so the planner's own task doesn't count as "backlog" for
    the purpose of the staleness check.

    Tasks that are **children of an approved plan_project task** are
    also excluded. These are the architect's own emit output — they
    exist *because of* the plan, so the fact that they're newer than
    plan-approved timestamp is expected, not staleness. Without this
    exclusion, every just-approved plan looks stale relative to its
    own 18-ish implementation tasks and the gate wrongly blocks
    delegation (Notesy, 2026-04-23).
    """
    try:
        tasks = list(work_service.list_tasks(project=project_key))
    except Exception:  # noqa: BLE001
        logger.debug(
            "plan_presence: list_tasks failed for project %s", project_key,
            exc_info=True,
        )
        return None

    # Collect the task-ids that were emitted as children of any
    # approved plan_project task so we can skip them below.
    plan_child_keys: set[tuple[str, int]] = set()
    for task in tasks:
        if task.flow_template_id not in _PLANNING_FLOWS:
            continue
        for child in getattr(task, "children", None) or []:
            try:
                child_project, child_number = child
                plan_child_keys.add((str(child_project), int(child_number)))
            except (TypeError, ValueError):
                continue

    latest: float | None = None
    for task in tasks:
        flow_id = getattr(task, "flow_template_id", "") or ""
        if flow_id in _PLANNING_FLOWS:
            continue
        if flow_id in _NON_BACKLOG_FLOWS:
            continue
        labels = set(getattr(task, "labels", None) or [])
        if labels & _NON_BACKLOG_LABELS:
            continue
        key = (project_key, getattr(task, "task_number", -1))
        if key in plan_child_keys:
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
    closed on any error — if we can't read the plan file, or we can't
    derive the plan's approval timestamp, the gate denies.

    Callers should cache results per ``(project_key, sweep_tick)`` to
    avoid repeated disk reads across the many-queued-tasks case.
    """
    if work_service is None:
        return False
    if _first_non_trivial_plan_path(project_path, plan_dir) is None:
        return False
    plan_task = _find_approved_plan_task(work_service, project_key)
    if plan_task is None:
        return False
    # Staleness check (#281): the plan_approved timestamp must be at
    # least as recent as the newest non-planning task. Timestamp comes
    # from the ``plan_approved`` context entry written at approve()
    # time; falls back to the user_approval execution's completed_at
    # for plans approved pre-#281. If there's no backlog yet, nothing
    # to be stale against. File mtime is intentionally NOT used —
    # it's perturbed by git checkouts, editor saves, and the
    # planner's own stage-8 emit.
    latest_backlog = _latest_backlog_created_at(work_service, project_key)
    if latest_backlog is not None:
        plan_approved_ts = _plan_approved_at(work_service, plan_task)
        if plan_approved_ts is None:
            return False
        if plan_approved_ts < latest_backlog:
            return False
    return True


def plan_blocked_task_ids(
    project_key: str,
    project_path: Path,
    work_service: Any,
    *,
    plan_dir: str = "docs/plan",
) -> set[str]:
    """Return queued task ids currently blocked by the plan gate.

    This is the read-side companion to the sweeper's plan gate. It does
    not persist a synthetic status; callers can derive
    ``waiting_on_plan`` for display from the same predicate that blocks
    pickup pings.
    """
    try:
        acceptable = has_acceptable_plan(
            project_key,
            project_path,
            work_service,
            plan_dir=plan_dir,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "plan_presence: gate evaluation failed for project %s",
            project_key,
            exc_info=True,
        )
        acceptable = False
    if acceptable:
        return set()
    try:
        tasks = work_service.list_tasks(project=project_key)
    except Exception:  # noqa: BLE001
        logger.debug(
            "plan_presence: list_tasks failed for project %s", project_key,
            exc_info=True,
        )
        return set()
    blocked: set[str] = set()
    for task in tasks:
        status = getattr(getattr(task, "work_status", None), "value", None) or str(
            getattr(task, "work_status", "") or ""
        )
        if status != WorkStatus.QUEUED.value:
            continue
        if task_bypasses_plan_gate(task):
            continue
        task_id = getattr(task, "task_id", None)
        if task_id:
            blocked.add(str(task_id))
    return blocked


def plan_approval_task(work_service: Any, project_key: str) -> Any | None:
    """Return the best plan task for a user to approve next.

    Preference is given to an active ``plan_project`` task sitting on
    the canonical ``user_approval`` node. If no such task exists, return
    the newest non-terminal plan task so display surfaces a concrete
    place to continue planning instead of a dead-end "missing plan"
    message.
    """
    try:
        tasks = list(work_service.list_tasks(project=project_key))
    except Exception:  # noqa: BLE001
        logger.debug(
            "plan_presence: list_tasks failed for project %s", project_key,
            exc_info=True,
        )
        return None
    plan_tasks = [
        task for task in tasks
        if getattr(task, "flow_template_id", "") == "plan_project"
    ]
    if not plan_tasks:
        return None
    terminal = {WorkStatus.DONE.value, WorkStatus.CANCELLED.value}
    open_plan_tasks = [
        task for task in plan_tasks
        if (
            getattr(getattr(task, "work_status", None), "value", None)
            or str(getattr(task, "work_status", "") or "")
        ) not in terminal
    ]
    if not open_plan_tasks:
        return None

    def _sort_value(task: Any) -> float:
        stamp = getattr(task, "updated_at", None) or getattr(task, "created_at", None)
        if stamp is None:
            return 0.0
        try:
            return float(stamp.timestamp())
        except Exception:  # noqa: BLE001
            return 0.0

    open_plan_tasks.sort(key=_sort_value, reverse=True)
    for task in open_plan_tasks:
        if getattr(task, "current_node_id", None) == _APPROVAL_NODE:
            return task
    return open_plan_tasks[0]


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
