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
for a queued non-planning task, the sweeper consults
``has_acceptable_plan`` — a project without an approved, non-trivial
``docs/plan/plan.md`` blocks *new delegation* until planning is done.
Tasks already in ``review`` / ``in_progress`` keep their recovery
re-pings even if the plan later goes stale or disappears; the gate is
only about admitting new queued work. Blocked projects get a single
deduped ``plan_missing`` alert per sweep cycle. Planning tasks
(``plan_project`` / ``critique_flow``) and tasks labelled
``bypass_plan_gate`` skate past the gate so the planner can always
run and operators can force delegation when needed.
"""

from __future__ import annotations

import logging
import re
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
    SWEEPER_COOLDOWN_SECONDS,
    _mark_kickoff_delivered,
    load_runtime_services,
    notify,
)

logger = logging.getLogger(__name__)

_TASK_ID_PATTERN = re.compile(r"\b([A-Za-z0-9_.-]+/\d+)\b")

# Work statuses the sweeper cares about — those where a machine actor is
# the expected next mover. ``in_progress`` / ``rework`` are gated on an
# idleness check (see ``_target_session_is_idle``) so we don't spam an
# actively-turning worker; ``queued`` and ``review`` are always safe to
# re-emit (dedupe handles throttling).
_SWEEPABLE_STATUSES = (
    WorkStatus.QUEUED.value,
    WorkStatus.REVIEW.value,
    WorkStatus.IN_PROGRESS.value,
    WorkStatus.REWORK.value,
)

_ACTIVE_WORKER_STATUSES = (
    WorkStatus.IN_PROGRESS.value,
    WorkStatus.REWORK.value,
)

# Statuses where an idle-session gate is required before notifying. The
# queued / review case is a new-or-pending assignment — pinging a busy
# session is fine because the ping just surfaces in their queue. The
# in_progress/rework cases mean the worker claimed + started work, so
# only re-ping when they've gone idle (supervisor restart, Claude
# relaunched with no context, etc.).
_IDLE_GATED_STATUSES = frozenset(_ACTIVE_WORKER_STATUSES)

# Per-task context marker written when a recurring sweeper emits (or
# dedupes) a task assignment notification. Cockpit derives a transient
# "recently pinged" badge from entries newer than this window.
SWEEPER_PING_CONTEXT_ENTRY_TYPE = "sweeper_ping"
RECENT_SWEEPER_PING_SECONDS = 60


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
        # #921: include ``task_number`` so per-task ``task-<proj>-<N>``
        # windows resolve as the worker session.
        handle = index.resolve(
            event.actor_type,
            event.actor_name,
            event.project,
            task_number=event.task_number,
        )
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


def _kickoff_pending(work_service: Any, event: TaskAssignmentEvent) -> bool:
    """Return True when this worker kickoff has not yet been delivered.

    #922: gates the sweep's "force the first push" branch. We only
    care about the ``actor_type=ROLE, actor_name=worker`` case — the
    legacy long-lived workers (reviewer / operator / heartbeat) and
    agent-pinned nodes don't suffer from the bootstrap race because
    their target session is already running when the assignment
    fires.

    Returns False on any error or when the work service doesn't
    expose ``kickoff_sent_at`` (test doubles, pre-#922 builds) — that
    keeps the existing idle-gated + throttled behaviour as the
    fallback. A queued task carries no execution row yet (visit=0),
    which the helper also treats as "not pending" — the ``queued``
    branch already pings reliably via the in-process listener +
    sweep, and we don't want to forcibly re-fire it past the dedupe.
    """
    if event.work_status not in _ACTIVE_WORKER_STATUSES:
        return False
    from pollypm.work.models import ActorType

    if event.actor_type is not ActorType.ROLE:
        return False
    if (event.actor_name or "").strip().lower() != "worker":
        return False
    getter = getattr(work_service, "kickoff_sent_at", None)
    if not callable(getter):
        return False
    try:
        stamped = getter(
            event.project,
            event.task_number,
            event.current_node,
            event.execution_version or None,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment sweep: kickoff_sent_at lookup failed for %s",
            event.task_id, exc_info=True,
        )
        return False
    return stamped in (None, "")


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
    # #349: alerts now live on the unified ``messages`` table via the Store.
    store = services.msg_store or services.state_store
    if store is None:
        return
    # Candidate session name we would *expect* if the worker were running —
    # keeps the alert's session_name column aligned with the missing
    # session's identity, which is what the cockpit's "alerts for session X"
    # queries filter on.
    candidates = role_candidate_names(role, project) if actor_type is ActorType.ROLE else [role]
    expected_name = candidates[0] if candidates else f"{role}-{project}"
    # #760 — actionable single-line copy. Old phrasing started with
    # routing-engine language ("no live session for role:worker in
    # project X") which read as machine-to-machine. New phrasing
    # starts with the user-visible effect and names the concrete
    # command alternatives, with ``pm task claim`` first for worker-
    # role tasks (pm worker-start --role=worker is deprecated in
    # favour of per-task workers — see pm worker-start --help).
    if actor_type is ActorType.ROLE and role == "architect":
        fix_hint = f"Try: pm worker-start --role architect {project}"
    elif actor_type is ActorType.ROLE and role == "worker":
        fix_hint = f"Try: pm task claim {example_task_id}"
    elif actor_type is ActorType.ROLE:
        fix_hint = (
            f"Try: pm worker-start --role {role} {project} "
            f"(or pm task claim {example_task_id} for a per-task worker)"
        )
    else:
        fix_hint = f"Try: pm task claim {example_task_id}"
    message = (
        f"No worker is running for the {role} role on '{project}' — "
        f"task {example_task_id} is stuck in the queue. {fix_hint}"
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
    # #349: alerts now live on the unified ``messages`` table via the Store.
    store = services.msg_store or services.state_store
    if store is None:
        return
    # Alert row is keyed by the project identity — we use a synthetic
    # session_name ``plan_gate-<project>`` so the cockpit's per-session
    # alert view groups it alongside the project's worker alerts.
    session_name = _plan_missing_session_name(project)
    # #760 — actionable single-line copy: name the project + the
    # blocked task so the reader knows why it matters. The earlier
    # tail "Try: pm project plan <name>" pushed the user out of the
    # cockpit to a shell — replaced with the in-cockpit affordance
    # since c is now wired on the project Plan card (#866, #863).
    message = (
        f"Project '{project}' has no approved plan yet — "
        f"queued task {example_task_id} is waiting on it. "
        f"Open the project and press c to ask the PM to plan it."
    )
    try:
        store.upsert_alert(session_name, "plan_missing", "warn", message)
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment sweep: upsert_alert(plan_missing) failed for %s",
            session_name, exc_info=True,
        )


def _plan_missing_session_name(project: str) -> str:
    """Return the synthetic session name used for plan-gate alerts."""
    return f"plan_gate-{project}"


def _clear_plan_missing_alert(services: Any, *, project: str) -> None:
    """Clear the open ``plan_missing`` alert for ``project`` if present."""
    store = services.msg_store or services.state_store
    if store is None:
        return
    session_name = _plan_missing_session_name(project)
    try:
        store.clear_alert(session_name, "plan_missing")
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment sweep: clear_alert(plan_missing) failed for %s",
            session_name, exc_info=True,
        )


def _record_sweeper_ping(
    work: Any,
    task_id: str,
    *,
    outcome: str,
    source: str,
) -> None:
    """Stamp ``task_id`` with a recent sweeper-ping marker."""
    if outcome not in {"sent", "deduped"}:
        return
    add_context = getattr(work, "add_context", None)
    if not callable(add_context):
        return
    try:
        add_context(
            task_id,
            "sweeper",
            f"{source}:{outcome}",
            entry_type=SWEEPER_PING_CONTEXT_ENTRY_TYPE,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment sweep: failed recording sweeper ping for %s",
            task_id,
            exc_info=True,
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
    project: Any = None,
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
    global_enforce = bool(getattr(services, "enforce_plan", True))
    project_enforce = (
        getattr(project, "enforce_plan", None) if project is not None else None
    )
    enforce_plan = (
        project_enforce if project_enforce is not None else global_enforce
    )
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
            # #922: a worker kickoff that's still un-delivered (no
            # ``kickoff_sent_at`` stamp on the active execution row)
            # bypasses the idle gate AND the notify dedupe. The race
            # we're fixing is "in-process listener fired during claim
            # before the per-task pane existed (or while it was still
            # bootstrapping), so the first 'Resume work' ping never
            # landed; the standard sweep path then either skips with
            # ``skipped_active_turn`` (claude bullet ⏺ in pane text)
            # or dedupes against a poisoned record_notification row".
            # Forcing the kickoff here is safe because the stamp is
            # idempotent — once set, future ticks fall back to the
            # normal idle-gated + 5-min-throttled path.
            kickoff_pending = _kickoff_pending(work, event)
            # #246: for in_progress tasks, only ping if the worker
            # session is idle. An active turn means they're working;
            # resume pings are for the restart / crash-recovery case.
            if status in _IDLE_GATED_STATUSES and not kickoff_pending:
                if not _target_session_is_idle(event, services):
                    by_outcome["skipped_active_turn"] = (
                        by_outcome.get("skipped_active_turn", 0) + 1
                    )
                    continue

            # #273: plan-presence gate. Only queued delegation is
            # blocked — review / in_progress items are already in
            # flight and still need recovery pings if the daemon or
            # target session restarts later. We only apply the gate
            # for per-project sweeps (``project_path`` supplied)
            # because workspace-root tasks have no single project path
            # to anchor to.
            if (
                enforce_plan
                and project_path is not None
                and status == WorkStatus.QUEUED.value
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
            # #922: when the kickoff hasn't been delivered yet, drop
            # the throttle to 0 so the dedupe table doesn't suppress
            # the first push. The stamp landing on success keeps the
            # next sweep tick on the normal throttle.
            effective_throttle = 0 if kickoff_pending else throttle_override
            result = notify(
                event, services=services, throttle_seconds=effective_throttle,
            )
            outcome = str(result.get("outcome", "unknown"))
            if kickoff_pending and outcome == "sent":
                by_outcome["forced_kickoff"] = (
                    by_outcome.get("forced_kickoff", 0) + 1
                )
            # #923: the sweep is the sole writer of ``kickoff_sent_at``.
            # ``notify()`` no longer stamps from the transition-time
            # listener (the per-task pane often hasn't finished
            # bootstrapping when the claim event fires, so the keystrokes
            # are silently dropped while the stamp lies about delivery).
            # Stamping here — after a confirmed-target ``sent`` — means
            # a re-resolved + re-sent ping is what marks the kickoff as
            # delivered, not an opportunistic transition-time call.
            if outcome == "sent":
                _mark_kickoff_delivered(event, work)
            by_outcome[outcome] = by_outcome.get(outcome, 0) + 1
            _record_sweeper_ping(
                work,
                event.task_id,
                outcome=outcome,
                source="task_assignment.sweep",
            )

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


# ---------------------------------------------------------------------------
# Auto-claim (#768)
# ---------------------------------------------------------------------------


def _auto_claim_enabled_for_project(services: Any, project: Any) -> bool:
    """Return True when this project is eligible for auto-claim.

    Precedence: per-project opt-out > global flag. Per-project ``None``
    means "defer to global default" — only an explicit ``False`` turns
    the project off.
    """
    if not getattr(services, "auto_claim", True):
        return False
    project_flag = getattr(project, "auto_claim", None)
    if project_flag is False:
        return False
    return True


def _max_concurrent_for_project(services: Any, project: Any) -> int:
    """Resolve the worker-concurrency cap for a project.

    Per-project override wins; otherwise the global ``max_concurrent_per_project``.
    Always returns at least 1 so an explicitly-zero config doesn't
    silently disable claims (use ``auto_claim=false`` for that).
    """
    override = getattr(project, "max_concurrent_workers", None)
    if isinstance(override, int) and override > 0:
        return override
    return max(1, int(getattr(services, "max_concurrent_per_project", 2)))


def _tmux_window_alive_for_task(
    services: Any, project_key: str, task_number: int,
) -> bool:
    """Check whether the per-task tmux window for a claim is still alive.

    Window-naming contract: per-task workers land in a window whose
    name comes from :func:`pollypm.work.session_manager.task_window_name`
    (``task-<project>-<N>``). The recovery check compares against that
    exact name, not a substring match — a sibling project window like
    ``task-web_app-7`` was previously accepted as proof that ``app/7``
    was alive because ``"app" in "task-web_app-7"`` is true (#807).

    Any error returns True so we don't incorrectly reap a live worker
    on a transient query failure.
    """
    from pollypm.work.session_manager import task_window_name

    session_service = getattr(services, "session_service", None)
    if session_service is None:
        return True
    expected_name = task_window_name(project_key, task_number)
    try:
        tmux = getattr(session_service, "tmux", None)
        if tmux is None:
            return True
        target_session = getattr(session_service, "storage_closet_session_name", None)
        if callable(target_session):
            session_name = target_session()
        else:
            session_name = "pollypm-storage-closet"
        windows = tmux.list_windows(session_name)
    except Exception:  # noqa: BLE001
        return True
    for window in windows or []:
        name = getattr(window, "name", "") or ""
        if name == expected_name and not getattr(window, "pane_dead", False):
            return True
    return False


def _recover_dead_claims(
    services: Any,
    work: Any,
    project: Any,
    totals: dict[str, Any],
) -> None:
    """Unclaim active worker-role tasks whose tmux window is gone.

    For each active worker task (``in_progress`` or ``rework``) in this
    project, verify the per-task tmux window still exists. If it doesn't
    (crashed session, closed window, host reboot), clear the stale claim
    state and walk the task back through queued so it becomes eligible
    for auto-claim or a manual ``pm task claim`` on the next sweep tick.
    """
    project_key = getattr(project, "key", None)
    if not project_key:
        return
    active_tasks: list[Any] = []
    for status in _ACTIVE_WORKER_STATUSES:
        try:
            active_tasks.extend(work.list_tasks(project=project_key, work_status=status))
        except Exception:  # noqa: BLE001
            continue
    by_outcome = totals["by_outcome"]
    for task in active_tasks:
        roles = getattr(task, "roles", {}) or {}
        if "worker" not in roles:
            continue
        task_number = getattr(task, "task_number", None)
        if task_number is None:
            continue
        if _tmux_window_alive_for_task(services, project_key, task_number):
            continue
        # Window is gone — release the claim back to queued.
        task_id = getattr(task, "task_id", f"{project_key}/{task_number}")
        try:
            release = getattr(work, "release_stale_claim", None)
            if not callable(release):
                raise RuntimeError(
                    "work service does not support release_stale_claim"
                )
            release(
                task_id,
                "auto_claim_sweep",
                reason="worker session missing",
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "task_auto_claim: stale claim release failed for %s",
                task_id, exc_info=True,
            )
            continue
        by_outcome["auto_claim_recovered"] = (
            by_outcome.get("auto_claim_recovered", 0) + 1
        )
        # Record an event so the activity log shows the auto-recovery.
        msg_store = getattr(services, "msg_store", None)
        if msg_store is not None:
            try:
                msg_store.append_event(
                    scope=project_key,
                    sender="auto_claim_sweep",
                    subject="worker_session_recovered",
                    payload={
                        "task_id": task_id,
                        "reason": "tmux window missing; task returned to queued",
                    },
                )
            except Exception:  # noqa: BLE001
                pass


def _auto_claim_next(
    services: Any,
    work: Any,
    project: Any,
    totals: dict[str, Any],
    *,
    plan_missing_projects: set[str] | None = None,
) -> None:
    """Claim the next queued worker-role task if capacity allows.

    Called once per project per sweep tick after the regular sweep body
    has run. Rate-limited by the per-project ``max_concurrent_workers``
    so overnight runs don't burn every Claude session at once on a
    freshly-emitted plan.
    """
    project_key = getattr(project, "key", None)
    if not project_key:
        return
    project_path = getattr(project, "path", None)
    if project_path is None:
        return
    # Capacity check. We count active worker-role tasks rather than
    # tmux ``worker_sessions`` rows because the DB claim is the
    # authoritative signal: a claim without a spawned session is still
    # "work in flight" (the recovery helper above will unclaim it if
    # the tmux window never shows up).
    in_progress: list[Any] = []
    for status in _ACTIVE_WORKER_STATUSES:
        try:
            in_progress.extend(work.list_tasks(project=project_key, work_status=status))
        except Exception:  # noqa: BLE001
            continue
    active = [
        task for task in in_progress
        if "worker" in (getattr(task, "roles", {}) or {})
    ]
    cap = _max_concurrent_for_project(services, project)
    if len(active) >= cap:
        return

    # Pick the oldest queued worker-role task.
    try:
        queued = work.list_tasks(
            project=project_key, work_status=WorkStatus.QUEUED.value,
        )
    except Exception:  # noqa: BLE001
        return
    candidates = [
        task for task in queued
        if "worker" in (getattr(task, "roles", {}) or {})
    ]
    if not candidates:
        return
    # Sort by task_number ascending so deterministic pickup order.
    candidates.sort(key=lambda t: getattr(t, "task_number", 0))
    target = candidates[0]
    task_id = getattr(target, "task_id", None)
    if not task_id:
        return

    # Gate: plan must be approved before we can claim for the project.
    # Run this after finding a real queued candidate so a closed gate
    # produces an actionable alert tied to the blocked task instead of a
    # silent no-op in the auto-claim path.
    global_enforce = bool(getattr(services, "enforce_plan", True))
    project_enforce = getattr(project, "enforce_plan", None)
    enforce_plan = (
        project_enforce if project_enforce is not None else global_enforce
    )
    if enforce_plan and not task_bypasses_plan_gate(target):
        try:
            if not has_acceptable_plan(
                project_key, Path(project_path), work,
                plan_dir=getattr(services, "plan_dir", "docs/plan"),
            ):
                totals["by_outcome"]["auto_claim_skipped_plan_missing"] = (
                    totals["by_outcome"].get("auto_claim_skipped_plan_missing", 0) + 1
                )
                _emit_plan_missing_alert(
                    services,
                    project=project_key,
                    example_task_id=task_id,
                )
                if plan_missing_projects is not None:
                    plan_missing_projects.add(project_key)
                return
        except Exception:  # noqa: BLE001
            return

    try:
        work.claim(task_id, "auto_claim_sweep")
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "task_auto_claim: claim(%s) failed: %s", task_id, exc,
            exc_info=True,
        )
        totals["by_outcome"]["auto_claim_failed"] = (
            totals["by_outcome"].get("auto_claim_failed", 0) + 1
        )
        return
    totals["by_outcome"]["auto_claim_spawned"] = (
        totals["by_outcome"].get("auto_claim_spawned", 0) + 1
    )
    msg_store = getattr(services, "msg_store", None)
    if msg_store is not None:
        try:
            msg_store.append_event(
                scope=project_key,
                sender="auto_claim_sweep",
                subject="worker_auto_claimed",
                payload={
                    "task_id": task_id,
                    "active_workers_before": len(active),
                    "cap": cap,
                },
            )
        except Exception:  # noqa: BLE001
            pass


def _wire_session_manager(svc: Any, project_root: Path, services: Any) -> None:
    """Best-effort session-manager wiring for a project-scoped work service."""
    try:
        from pollypm.session_services import create_tmux_client
        from pollypm.work.session_manager import SessionManager

        if project_root.exists() and (project_root / ".git").exists():
            session_mgr = SessionManager(
                tmux_client=create_tmux_client(),
                work_service=svc,
                project_path=project_root,
                config=getattr(services, "config", None),
                session_service=getattr(services, "session_service", None),
                storage_closet_name=getattr(
                    services,
                    "storage_closet_name",
                    "pollypm-storage-closet",
                ),
            )
            svc.set_session_manager(session_mgr)
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment sweep: failed to wire session manager for %s",
            project_root,
            exc_info=True,
        )


def _open_workspace_project_work_service(project: Any, services: Any) -> Any | None:
    """Open the workspace-root DB with a project-specific runtime context."""
    project_path = getattr(project, "path", None)
    if project_path is None:
        return None
    db_path = (
        Path(getattr(services, "project_root", Path.cwd()))
        / ".pollypm"
        / "state.db"
    )
    if not db_path.exists():
        return None
    try:
        from pollypm.work.sqlite_service import SQLiteWorkService

        project_root = Path(project_path)
        svc = SQLiteWorkService(db_path=db_path, project_path=project_root)
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment sweep: failed to open workspace DB at %s",
            db_path, exc_info=True,
        )
        return None
    _wire_session_manager(svc, project_root, services)
    return svc


def _open_project_work_service(project: Any, services: Any) -> Any | None:
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

        svc = SQLiteWorkService(db_path=db_path, project_path=Path(project_path))
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment sweep: failed to open per-project DB at %s",
            db_path, exc_info=True,
        )
        return None

    _wire_session_manager(svc, Path(project_path), services)
    return svc


def _close_quietly(svc: Any) -> None:
    closer = getattr(svc, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # noqa: BLE001
            pass


def _park_review_tasks_waiting_on_notify(
    work: Any,
    *,
    project_key: str,
    totals: dict[str, Any],
) -> None:
    """Keep notify-driven review tasks in ``review``.

    Reviewer/operator notifies are already visible in the inbox; they
    should not demote a task out of ``review`` because that hides the
    accept/reject controls and makes the next required action ambiguous.
    """
    _ = work, project_key, totals


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

    # Workspace-root task DBs can contain tasks for any registered
    # project. Reopen that same DB once per project with the project's
    # filesystem path and session manager before auto-claiming; a generic
    # workspace-root work service cannot provision a project-scoped worker.
    for project in services.known_projects:
        if not _auto_claim_enabled_for_project(services, project):
            continue
        workspace_project_work = _open_workspace_project_work_service(
            project, services,
        )
        if workspace_project_work is None:
            continue
        try:
            _recover_dead_claims(services, workspace_project_work, project, totals)
            _auto_claim_next(
                services,
                workspace_project_work,
                project,
                totals,
                plan_missing_projects=plan_missing_projects,
            )
        finally:
            _close_quietly(workspace_project_work)

    # Pass 2: per-project DBs. Each gets its own connection, opened and
    # closed within the sweep tick so we don't pile up file handles
    # when many projects are registered.
    for project in services.known_projects:
        project_key = getattr(project, "key", None)
        project_work = _open_project_work_service(project, services)
        if project_work is None:
            projects_skipped += 1
            if project_key and project_key not in plan_missing_projects:
                _clear_plan_missing_alert(services, project=project_key)
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
                project=project,
            )
            # #768: auto-claim runs after the regular sweep body so
            # dead-window recovery has the most-recent state to work
            # with. Skipped entirely for projects that have opted out
            # (``[projects.<key>].auto_claim = false``) or when the
            # global flag is disabled.
            if _auto_claim_enabled_for_project(services, project):
                _recover_dead_claims(services, project_work, project, totals)
            if project_key:
                _park_review_tasks_waiting_on_notify(
                    project_work,
                    project_key=project_key,
                    totals=totals,
                )
            if _auto_claim_enabled_for_project(services, project):
                _auto_claim_next(
                    services,
                    project_work,
                    project,
                    totals,
                    plan_missing_projects=plan_missing_projects,
                )
            if project_key and project_key not in plan_missing_projects:
                _clear_plan_missing_alert(services, project=project_key)
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
