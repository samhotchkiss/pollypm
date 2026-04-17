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

#259: the sweeper now fans out across per-project work-service DBs.
``pm project new`` creates tasks in ``<project_path>/.pollypm/state.db``
— those are invisible to a sweep that only reads the workspace-root
DB, so pickup pings never fire for operator-created projects. We
enumerate ``config.projects`` (exposed as ``services.known_projects``)
and run the same sweep body against each per-project DB, opening and
closing the connection per tick. When a project has queued tasks for
a role that has no live session, we emit a single deduped
``no_session`` alert per ``(project, role)`` — surfacing the blocked
worker to the operator instead of silently dropping pings.

#273: project-level plan-presence enforcement. Before emitting a ping
for any non-planning task, the sweeper consults
``has_acceptable_plan`` — a project without an approved, non-trivial
``docs/plan/plan.md`` blocks delegation entirely. Blocked projects
get a single deduped ``plan_missing`` alert per sweep cycle. Planning
tasks (``plan_project`` / ``critique_flow``) and tasks labelled
``bypass_plan_gate`` skate past the gate so the planner can always
run and operators can force delegation when needed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pollypm.work.models import ActorType, WorkStatus
from pollypm.work.task_assignment import (
    SessionRoleIndex,
    TaskAssignmentEvent,
    role_candidate_names,
)

from pollypm.plugins_builtin.project_planning.plan_presence import (
    has_acceptable_plan,
    task_bypasses_plan_gate,
)
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
    # #279: look up the current execution's visit number so the dedupe
    # key includes ``(session, task, execution_version)``. A rejection
    # that bounces the task back to an earlier node opens a fresh visit,
    # which correctly lets the retry ping through even inside the 30-min
    # window. Best-effort — missing helper / errors fall back to 0,
    # matching the pre-migration default for existing dedupe rows.
    execution_version = 0
    visit_fn = getattr(work_service, "current_node_visit", None)
    if callable(visit_fn):
        try:
            execution_version = int(
                visit_fn(task.project, task.task_number, node_id) or 0
            )
        except Exception:  # noqa: BLE001
            execution_version = 0
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
        execution_version=execution_version,
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


def _emit_no_session_alert(
    services: Any,
    *,
    project: str,
    role: str,
    actor_type: ActorType,
    example_task_id: str,
) -> None:
    """Raise (or refresh) a ``no_session`` alert for a ``(project, role)``.

    Sweep-level alert — complements the per-task ``no_session_for_assignment:<id>``
    alerts from ``_escalate_no_session`` with a single human-readable row
    per blocked role. The underlying ``upsert_alert`` already dedupes on
    ``(session_name, alert_type, status='open')`` so repeat emissions
    within a sweep cycle (or across sweep cycles) just refresh the row.
    """
    store = services.state_store
    if store is None:
        return
    # Candidate session name we would *expect* if the worker were running —
    # keeps the alert's session_name column aligned with the missing
    # session's identity, which is what the cockpit's "alerts for session X"
    # queries filter on.
    candidates = role_candidate_names(role, project) if actor_type is ActorType.ROLE else [role]
    expected_name = candidates[0] if candidates else f"{role}-{project}"
    message = (
        f"Queued task {example_task_id} has no live session for "
        f"{actor_type.value}:{role} in project '{project}'. "
        f"Fix: pm worker-start {project}"
    )
    try:
        store.upsert_alert(expected_name, "no_session", "warn", message)
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment sweep: upsert_alert(no_session) failed for %s",
            expected_name, exc_info=True,
        )


def _emit_plan_missing_alert(
    services: Any,
    *,
    project: str,
    example_task_id: str,
) -> None:
    """Raise (or refresh) a ``plan_missing`` alert for a project.

    #273 sweep-level alert — fires once per project per sweep cycle
    when the plan-presence gate blocks delegation. Keyed by
    ``(project, 'plan_missing')`` so a project with many queued tasks
    produces one row instead of N. Mirrors the ``_emit_no_session_alert``
    dedupe semantics (``upsert_alert`` refreshes rather than duplicates).
    """
    store = services.state_store
    if store is None:
        return
    # Alert row is keyed by the project identity — we use a synthetic
    # session_name ``plan_gate-<project>`` so the cockpit's per-session
    # alert view groups it alongside the project's worker alerts.
    session_name = f"plan_gate-{project}"
    message = (
        f"Queued task {example_task_id} in project '{project}' cannot "
        f"be delegated — no approved plan found at docs/plan/plan.md. "
        f"Fix: pm project plan {project}"
    )
    try:
        store.upsert_alert(session_name, "plan_missing", "warn", message)
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment sweep: upsert_alert(plan_missing) failed for %s",
            session_name, exc_info=True,
        )


def _plan_gate_allows(
    project_key: str,
    project_path: Any,
    work_service: Any,
    services: Any,
    plan_decisions: dict[str, bool],
) -> bool:
    """Return True when the plan gate allows delegation for ``project_key``.

    Per-sweep-tick cache — the predicate reads ``plan.md`` from disk
    and queries ``list_tasks``, so we amortise across the many-queued-
    tasks case by memoising the decision keyed by project key alone.
    Values: True = gate open (plan acceptable), False = gate closed.
    """
    cached = plan_decisions.get(project_key)
    if cached is not None:
        return cached
    if project_path is None:
        # Without a filesystem anchor we can't check ``docs/plan/plan.md``
        # — treat the project as gated (fail closed).
        plan_decisions[project_key] = False
        return False
    try:
        allowed = has_acceptable_plan(
            project_key,
            Path(project_path),
            work_service,
            plan_dir=getattr(services, "plan_dir", "docs/plan"),
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment sweep: plan gate evaluation failed for %s",
            project_key, exc_info=True,
        )
        allowed = False
    plan_decisions[project_key] = allowed
    return allowed


def _sweep_work_service(
    work: Any,
    services: Any,
    *,
    throttle_override: int,
    totals: dict[str, Any],
    alerted_pairs: set[tuple[str, str]],
    plan_missing_projects: set[str],
    plan_decisions: dict[str, bool],
    project_path: Any = None,
) -> None:
    """Run one sweep pass over a single work-service DB.

    Mutates ``totals`` (``considered`` count + ``by_outcome`` tally) in
    place so the caller can aggregate across per-project DBs. Tracks
    already-alerted ``(project, role)`` pairs in ``alerted_pairs`` so
    we emit at most one ``no_session`` alert per pair per sweep cycle.

    ``plan_missing_projects`` — projects we've already emitted a
    ``plan_missing`` alert for in this sweep cycle. ``plan_decisions``
    — per-project plan-gate decision cache, one entry per project.
    ``project_path`` — filesystem anchor for the plan-presence check;
    None for the workspace-root pass (which has no single project
    root and so auto-skips the gate).
    """
    by_outcome = totals["by_outcome"]
    enforce_plan = bool(getattr(services, "enforce_plan", True))
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

            # #273: plan-presence gate. The planner's own tasks
            # bypass; everyone else is blocked until the project has
            # an approved plan. We only apply the gate for per-project
            # sweeps (``project_path`` supplied) because workspace-root
            # tasks have no single project path to anchor to.
            if (
                enforce_plan
                and project_path is not None
                and not task_bypasses_plan_gate(task)
            ):
                if not _plan_gate_allows(
                    event.project,
                    project_path,
                    work,
                    services,
                    plan_decisions,
                ):
                    by_outcome["skipped_plan_missing"] = (
                        by_outcome.get("skipped_plan_missing", 0) + 1
                    )
                    if event.project not in plan_missing_projects:
                        plan_missing_projects.add(event.project)
                        _emit_plan_missing_alert(
                            services,
                            project=event.project,
                            example_task_id=event.task_id,
                        )
                    continue

            totals["considered"] += 1
            result = notify(
                event, services=services, throttle_seconds=throttle_override,
            )
            outcome = str(result.get("outcome", "unknown"))
            by_outcome[outcome] = by_outcome.get(outcome, 0) + 1

            # #259: when the task can't be routed, also raise a
            # sweep-level ``no_session`` alert keyed by (project, role)
            # so the operator sees one row per blocked role instead of
            # N per-task alerts. Dedupe within a cycle via the visited
            # set; upsert_alert dedupes across cycles.
            if outcome == "no_session":
                pair = (event.project, event.actor_name)
                if pair not in alerted_pairs:
                    alerted_pairs.add(pair)
                    _emit_no_session_alert(
                        services,
                        project=event.project,
                        role=event.actor_name,
                        actor_type=event.actor_type,
                        example_task_id=event.task_id,
                    )


def _open_project_work_service(project: Any) -> Any | None:
    """Open a per-project ``SQLiteWorkService`` if its state.db exists.

    Returns ``None`` when the project path has no state.db yet (fresh
    registration, never-touched project) or when any open-time error
    prevents connecting. Never raises — the sweeper skips silently and
    moves on to the next project.
    """
    project_path = getattr(project, "path", None)
    if project_path is None:
        return None
    db_path = Path(project_path) / ".pollypm" / "state.db"
    if not db_path.exists():
        return None
    try:
        from pollypm.work.sqlite_service import SQLiteWorkService

        return SQLiteWorkService(db_path=db_path, project_path=Path(project_path))
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment sweep: failed to open per-project DB at %s",
            db_path, exc_info=True,
        )
        return None


def _close_quietly(svc: Any) -> None:
    closer = getattr(svc, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # noqa: BLE001
            pass


def task_assignment_sweep_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Re-notify machine-actor tasks in queued/review/in_progress states.

    Fans out across the workspace-root DB *and* every registered
    per-project DB (``config.projects``) so tasks created via
    ``pm project new`` are picked up. Each per-project connection is
    opened, read, and closed within the sweep tick — we don't hold
    20+ connections open permanently.
    """
    config_path_hint = payload.get("config_path")
    config_path = Path(config_path_hint) if config_path_hint else None
    services = load_runtime_services(config_path=config_path)

    # The sweeper uses a shorter throttle so pre-existing queued tasks
    # get re-pinged every 5 min if they stay unclaimed — that's the
    # "session came online late" recovery path from the spec.
    throttle_override = int(payload.get("throttle_seconds", SWEEPER_COOLDOWN_SECONDS))
    if throttle_override < 1:
        throttle_override = SWEEPER_COOLDOWN_SECONDS

    totals: dict[str, Any] = {"considered": 0, "by_outcome": {}}
    alerted_pairs: set[tuple[str, str]] = set()
    plan_missing_projects: set[str] = set()
    plan_decisions: dict[str, bool] = {}
    projects_scanned = 0
    projects_skipped = 0

    # Pass 1: workspace-root DB (workspace-level tasks the pollypm repo
    # itself uses, or tests that point services.work_service at a
    # tmpdir without registered projects). Workspace-root tasks aren't
    # anchored to a single project directory, so the plan-presence
    # gate is intentionally skipped for this pass (``project_path=None``).
    workspace_work = services.work_service
    if workspace_work is not None:
        try:
            _sweep_work_service(
                workspace_work, services,
                throttle_override=throttle_override,
                totals=totals,
                alerted_pairs=alerted_pairs,
                plan_missing_projects=plan_missing_projects,
                plan_decisions=plan_decisions,
                project_path=None,
            )
        finally:
            _close_quietly(workspace_work)
    elif not services.known_projects:
        # No workspace work service AND no registered projects → nothing
        # to sweep. Keep the legacy "no_work_service" outcome for
        # observability / existing callers.
        return {"outcome": "skipped", "reason": "no_work_service"}

    # Pass 2: per-project DBs. Each gets its own connection, opened and
    # closed within the sweep tick so we don't pile up file handles
    # when many projects are registered.
    for project in services.known_projects:
        project_work = _open_project_work_service(project)
        if project_work is None:
            projects_skipped += 1
            continue
        try:
            _sweep_work_service(
                project_work, services,
                throttle_override=throttle_override,
                totals=totals,
                alerted_pairs=alerted_pairs,
                plan_missing_projects=plan_missing_projects,
                plan_decisions=plan_decisions,
                project_path=getattr(project, "path", None),
            )
            projects_scanned += 1
        finally:
            _close_quietly(project_work)

    return {
        "outcome": "swept",
        "considered": totals["considered"],
        "by_outcome": totals["by_outcome"],
        "projects_scanned": projects_scanned,
        "projects_skipped": projects_skipped,
        "no_session_alerts": len(alerted_pairs),
        "plan_missing_alerts": len(plan_missing_projects),
    }
