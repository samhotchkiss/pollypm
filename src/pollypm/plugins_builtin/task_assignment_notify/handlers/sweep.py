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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pollypm.plan_presence import (
    compute_plan_missing_projects,
    has_acceptable_plan,
    task_bypasses_plan_gate,
)
from pollypm.plugins_builtin.task_assignment_notify.resolver import (
    SWEEPER_COOLDOWN_SECONDS,
    _known_project_keys,
    _mark_kickoff_delivered,
    load_runtime_services,
    notify,
)
from pollypm.work.models import ActorType, ExecutionStatus, WorkStatus
from pollypm.work.task_assignment import (
    RECENT_SWEEPER_PING_SECONDS,
    SessionRoleIndex,
    SWEEPER_PING_CONTEXT_ENTRY_TYPE,
    TaskAssignmentEvent,
    build_event_for_task,
    role_candidate_names,
)


# #1012 — auto-claim circuit breaker.
#
# When a task's per-task worker spawn fails repeatedly (no live tmux
# window after each ``release_stale_claim`` → re-claim cycle), the
# sweep used to keep retrying forever, accumulating one ``build vN
# abandoned`` row per cycle (~50s apart). ``bikepath/8`` racked up
# 60+ rows in ~7h before the user noticed.
#
# After ``MAX_CONSECUTIVE_ABANDONMENTS`` consecutive abandoned rows on
# the active node, ``_recover_dead_claims`` stops releasing the stale
# claim — the task stays ``in_progress`` with a dead assignee, and the
# project's ``worker-<project>/no_session`` warn alert is escalated to
# a ``worker-<project>/spawn_failed_persistent`` error so the user
# sees an actionable blocker rather than a silent grow-the-history
# loop.
#
# #1014 (Bug A) — lowered from 5 to 3 because the live ``bikepath/8``
# repro showed 91 wasted spawns get through despite #1012's streak
# breaker. We still bound the wasted retries even when the consecutive
# streak counter races with node-bouncing reject rounds: the rate
# breaker (see :data:`RATE_BREAKER_WINDOW_SECONDS` /
# :data:`RATE_BREAKER_MAX_ABANDONMENTS`) trips on N abandons in M
# minutes regardless of node transitions.
MAX_CONSECUTIVE_ABANDONMENTS = 3
SPAWN_FAILED_PERSISTENT_ALERT_TYPE = "spawn_failed_persistent"

# #1014 (Bug A) — rate-based companion to the consecutive-streak
# breaker. A spinning auto-claim loop fires roughly one ABANDONED row
# per 50s sweep tick. ``RATE_BREAKER_MAX_ABANDONMENTS`` abandons inside
# ``RATE_BREAKER_WINDOW_SECONDS`` seconds is enough signal to stop
# regardless of which node each visit landed on, so reject-bounces
# (which walk the task back and forth between build and review nodes)
# can't silently reset the streak counter and let the loop run forever.
RATE_BREAKER_WINDOW_SECONDS = 600  # 10 minutes
RATE_BREAKER_MAX_ABANDONMENTS = 5

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


def _build_event_for_task(work_service: Any, task: Any) -> TaskAssignmentEvent | None:
    """Compatibility wrapper for the plugin's historical helper name."""
    return build_event_for_task(work_service, task, transitioned_by="sweeper")


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
    # #1001: project-existence guard. When the registry is non-empty and
    # ``project`` isn't in it, the project is a ghost — drop the alert.
    # An empty registry preserves the legacy unrestricted behaviour for
    # tests and config-less runs.
    known_keys = _known_project_keys(services)
    if known_keys and project not in known_keys:
        return
    # Candidate session name we would *expect* if the worker were running —
    # keeps the alert's session_name column aligned with the missing
    # session's identity, which is what the cockpit's "alerts for session X"
    # queries filter on.
    candidates = role_candidate_names(role, project) if actor_type is ActorType.ROLE else [role]
    expected_name = candidates[0] if candidates else f"{role}-{project}"
    # #760 — actionable single-line copy. Old phrasing started with
    # routing-engine language ("no live session for role:worker in
    # project X") which read as machine-to-machine. Keep the fix path
    # inside the cockpit so alerts do not turn into CLI instructions.
    if actor_type is ActorType.ROLE and role == "architect":
        fix_hint = "Open Workers and start or recover the architect."
    elif actor_type is ActorType.ROLE and role == "worker":
        fix_hint = (
            "Open Tasks to inspect the queue; Polly will claim it when "
            "worker capacity is available, or use Workers to start capacity."
        )
    elif actor_type is ActorType.ROLE and role == "reviewer":
        fix_hint = "Open Tasks or Inbox and use Approve or Reject."
    elif actor_type is ActorType.ROLE:
        fix_hint = f"Open Workers and start or recover the {role} role."
    else:
        fix_hint = "Open Tasks; Polly will claim it when a matching worker is available."
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
    # blocked task so the reader knows why it matters. #1029 — restore
    # the ``pm project plan <X>`` CLI verb as the lower-friction path
    # for users reading ``pm alerts`` cold; keep the cockpit affordance
    # (press ``c`` on the project Plan card, #866/#863) for users who
    # are already there. Spelling out "left rail" + "dashboard" makes
    # the keystroke context unambiguous.
    message = (
        f"Project '{project}' has no approved plan yet — "
        f"queued task {example_task_id} is waiting. "
        f"Run `pm project plan {project}` to queue planning, or open "
        f"the cockpit, focus '{project}' in the left rail, and press "
        f"`c` on its dashboard."
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


def _review_pending_session_name(project: str, task_number: int) -> str:
    """Return the synthetic session name used for review-pending alerts.

    #1053: review-state tasks are work-for-the-human (approve / reject /
    inspect). The session_name pattern ``review-<project>-<N>`` lets the
    cockpit's per-session alert grouping sort these alongside other
    per-task signals while remaining idempotent in ``upsert_alert``.
    """
    return f"review-{project}-{task_number}"


def _emit_review_pending_alert(
    services: Any,
    *,
    project: str,
    task: Any,
) -> None:
    """Raise (or refresh) a ``review_pending`` alert for a review-state task.

    #1053 — mirror of :func:`_emit_plan_missing_alert`. Each task in
    ``review`` state produces one alert, keyed by the
    ``review-<project>-<N>`` session_name so repeat sweep ticks just
    refresh the row via ``upsert_alert``'s
    ``(session_name, alert_type, status='open')`` dedupe.
    """
    store = services.msg_store or services.state_store
    if store is None:
        return
    # #1001: project-existence guard. When the registry is non-empty and
    # ``project`` isn't in it, the project is a ghost — drop the alert.
    known_keys = _known_project_keys(services)
    if known_keys and project not in known_keys:
        return
    task_number = getattr(task, "task_number", None)
    if task_number is None:
        return
    title = getattr(task, "title", "") or ""
    task_n = f"{project}/{task_number}"
    session_name = _review_pending_session_name(project, int(task_number))
    message = (
        f"Task {task_n} is awaiting human review: \"{title}\". "
        f"Run `pm task approve {task_n}` to approve, "
        f"`pm task reject {task_n}` to reject, or "
        f"`pm task get {task_n}` to read the review summary."
    )
    try:
        store.upsert_alert(session_name, "review_pending", "warn", message)
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment sweep: upsert_alert(review_pending) failed for %s",
            session_name, exc_info=True,
        )


def _clear_review_pending_alert(
    services: Any, *, project: str, task_number: int,
) -> None:
    """Clear the open ``review_pending`` alert for a task if present.

    #1053 — fired when a task transitions out of ``review`` (approved,
    rejected back to rework, cancelled, etc.). Mirror of
    :func:`_clear_plan_missing_alert`.
    """
    store = services.msg_store or services.state_store
    if store is None:
        return
    session_name = _review_pending_session_name(project, int(task_number))
    try:
        store.clear_alert(session_name, "review_pending")
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment sweep: clear_alert(review_pending) failed for %s",
            session_name, exc_info=True,
        )


def _sweep_stale_review_pending_alerts(
    services: Any,
    seen: set[tuple[str, int]],
) -> int:
    """Close ``review_pending`` alerts for tasks no longer in ``review``.

    #1053 — companion to :func:`_emit_review_pending_alert`. Walks the
    open alert set looking for rows with ``alert_type ==
    'review_pending'`` whose ``session_name`` decodes to a
    ``(project, task_number)`` key that wasn't observed during this
    sweep cycle (because the task was approved / rejected / cancelled).
    Mirrors the ``alert.cleared`` lifecycle pattern from #1033.

    Returns the count of cleared alerts (observability — folded into
    the sweep handler's result dict).
    """
    store = services.msg_store or services.state_store
    if store is None:
        return 0
    pairs: list[tuple[str, str]] = []
    query = getattr(store, "query_messages", None)
    if callable(query):
        try:
            for row in query(type="alert", state="open"):
                if str(row.get("sender") or "") != "review_pending":
                    continue
                pairs.append(
                    (str(row.get("scope") or ""), "review_pending"),
                )
        except Exception:  # noqa: BLE001
            logger.debug(
                "task_assignment sweep: review_pending query_messages failed",
                exc_info=True,
            )
            pairs = []
    if not pairs:
        list_open = getattr(store, "open_alerts", None)
        if callable(list_open):
            try:
                for alert in list_open():
                    if str(getattr(alert, "alert_type", "") or "") != "review_pending":
                        continue
                    pairs.append(
                        (
                            str(getattr(alert, "session_name", "") or ""),
                            "review_pending",
                        ),
                    )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "task_assignment sweep: review_pending open_alerts failed",
                    exc_info=True,
                )
                return 0
    cleared = 0
    for scope, alert_type in pairs:
        decoded = _decode_review_pending_session_name(scope)
        if decoded is None:
            continue
        if decoded in seen:
            continue
        try:
            store.clear_alert(scope, alert_type)
            cleared += 1
        except Exception:  # noqa: BLE001
            logger.debug(
                "task_assignment sweep: clear_alert(review_pending) failed for %s",
                scope, exc_info=True,
            )
    return cleared


def _decode_review_pending_session_name(scope: str) -> tuple[str, int] | None:
    """Decode a ``review-<project>-<N>`` session_name back to its key.

    Returns ``None`` for any string that doesn't match the pattern (the
    trailing token must be a positive integer task number; the
    ``review-`` prefix is required). ``project`` keys may legally
    contain dashes themselves so we anchor on the trailing
    ``-<digits>`` and treat everything between the ``review-`` prefix
    and that suffix as the project key.
    """
    if not scope.startswith("review-"):
        return None
    body = scope[len("review-"):]
    head, sep, tail = body.rpartition("-")
    if not sep or not head or not tail:
        return None
    try:
        task_number = int(tail)
    except ValueError:
        return None
    if task_number <= 0:
        return None
    return head, task_number


# ---------------------------------------------------------------------------
# Worker-session-gap alerts (#1054)
# ---------------------------------------------------------------------------
#
# Sister of #1053. The auto-claim sweep can emit ``no_session`` alerts
# only when it actually visits a queued task and notify() reports
# ``no_session`` for it — but that path is gated on the plan-presence
# check, idle-session checks, the throttle, and a few other branches
# that can each silently skip the per-task ping. The result observed
# in #1054: ``polly_remote`` had four queued worker tasks but no
# ``worker_<project>`` (or ``worker-<project>``) tmux session, and
# nothing surfaced the gap to the user. The post-sweep pass below
# walks the registered projects after the main sweep has run, counts
# ``work_status='queued'`` rows per project, and emits a
# ``worker_session_gap`` alert for any project whose worker session is
# absent from the storage-closet's window list. Auto-clears when a
# matching window appears (or when the project's queue empties).


WORKER_SESSION_GAP_ALERT_TYPE = "worker_session_gap"


def _worker_session_gap_session_name(project: str) -> str:
    """Return the synthetic session name used for worker-gap alerts."""
    return f"worker_session_gap-{project}"


def _emit_worker_session_gap_alert(
    services: Any,
    *,
    project: str,
    n_queued: int,
) -> None:
    """Raise (or refresh) a ``worker_session_gap`` alert for ``project``.

    #1054 — fires when a project has queued tasks but no
    ``worker_<project>`` (or ``worker-<project>``) tmux window exists in
    the storage-closet session. Keyed by ``(project, worker_session_gap)``
    via the synthetic ``worker_session_gap-<project>`` session name so
    the cockpit's per-session alert view groups it with the project's
    other gap alerts. ``upsert_alert`` dedupes on ``(scope, alert_type,
    status='open')`` so repeat ticks just refresh the existing row.
    """
    store = services.msg_store or services.state_store
    if store is None:
        return
    session_name = _worker_session_gap_session_name(project)
    # #1059 — point at the per-task claim flow rather than the deprecated
    # ``pm worker-start <project>`` (which defaults to ``--role worker``
    # and now errors out). Per-task workers auto-spawn from ``pm task
    # claim``; ``pm task next -p <project>`` surfaces the next queued id.
    message = (
        f"Project {project} has {n_queued} queued tasks but no per-task "
        f"workers have claimed them.\n"
        f"Try: pm task next -p {project}     (highest-priority queued task)\n"
        f"     pm task claim <task-id>        (per-task worker auto-spawns)\n"
        f"Or `pm task hold {project}/{{N}}` to pause the tasks."
    )
    try:
        store.upsert_alert(
            session_name,
            WORKER_SESSION_GAP_ALERT_TYPE,
            "warn",
            message,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment sweep: upsert_alert(worker_session_gap) failed for %s",
            session_name, exc_info=True,
        )


def _clear_worker_session_gap_alert(services: Any, *, project: str) -> None:
    """Clear the open ``worker_session_gap`` alert for ``project``."""
    store = services.msg_store or services.state_store
    if store is None:
        return
    session_name = _worker_session_gap_session_name(project)
    try:
        store.clear_alert(session_name, WORKER_SESSION_GAP_ALERT_TYPE)
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment sweep: clear_alert(worker_session_gap) failed for %s",
            session_name, exc_info=True,
        )


def _storage_closet_window_names(services: Any) -> set[str] | None:
    """Return the live window names in the storage-closet, or ``None``.

    ``None`` means we couldn't enumerate (no session service, no tmux
    client, query failed) — callers must treat that as "unknown" and
    skip the gap check rather than emit a false-positive alert. An
    empty set is a real answer ("the storage closet is empty") and is
    returned as ``set()``.
    """
    session_svc = getattr(services, "session_service", None)
    if session_svc is None:
        return None
    tmux = getattr(session_svc, "tmux", None)
    if tmux is None:
        return None
    target_session = getattr(session_svc, "storage_closet_session_name", None)
    if callable(target_session):
        try:
            session_name = target_session()
        except Exception:  # noqa: BLE001
            return None
    else:
        session_name = "pollypm-storage-closet"
    try:
        windows = tmux.list_windows(session_name)
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment sweep: list_windows(%s) failed",
            session_name, exc_info=True,
        )
        return None
    names: set[str] = set()
    for window in windows or []:
        name = getattr(window, "name", "") or ""
        if not name:
            continue
        if getattr(window, "pane_dead", False):
            continue
        names.add(name)
    return names


def _project_has_worker_session(
    project: str,
    window_names: set[str],
) -> bool:
    """Return True when a worker-flavoured window exists for ``project``.

    Accepts both ``worker_<project>`` and ``worker-<project>`` naming
    conventions (the underscore form is the shipping default; the
    hyphen form ships in some legacy setups and in operator tooling).
    Per-task ``task-<project>-<N>`` windows also count — when at least
    one per-task worker exists for the project, the gap is closed
    (something is actively working on the queue).
    """
    candidates = role_candidate_names("worker", project)
    for name in candidates:
        if name in window_names:
            return True
    task_prefix = f"task-{project}-"
    for name in window_names:
        if name.startswith(task_prefix):
            suffix = name[len(task_prefix):]
            if suffix.isdigit():
                return True
    return False


def _count_queued_tasks_for_project(
    services: Any,
    project: Any,
) -> int:
    """Return the count of queued tasks for ``project``.

    Sums queued rows across the per-project DB (``<path>/.pollypm/state.db``)
    and the workspace-root DB if it exists — workspace tasks created via
    ``pm work create`` from the workspace root land in the workspace DB
    even when their ``project`` field points at a registered project.

    Returns 0 on any error so the helper degrades closed (a transient
    DB issue shouldn't fire a false-positive worker-session-gap alert).
    """
    project_key = getattr(project, "key", None)
    if not project_key:
        return 0
    total = 0
    project_work = _open_project_work_service(project, services)
    if project_work is not None:
        try:
            tasks = project_work.list_tasks(work_status=WorkStatus.QUEUED.value)
            total += len(list(tasks))
        except Exception:  # noqa: BLE001
            logger.debug(
                "task_assignment sweep: list_tasks(queued) failed for %s",
                project_key, exc_info=True,
            )
        finally:
            _close_quietly(project_work)
    workspace_work = services.work_service
    if workspace_work is not None:
        try:
            tasks = workspace_work.list_tasks(
                project=project_key,
                work_status=WorkStatus.QUEUED.value,
            )
            total += len(list(tasks))
        except Exception:  # noqa: BLE001
            logger.debug(
                "task_assignment sweep: workspace list_tasks(queued) "
                "failed for %s",
                project_key, exc_info=True,
            )
    return total


def _sweep_worker_session_gaps(services: Any) -> dict[str, int]:
    """Emit ``worker_session_gap`` alerts for projects with queued tasks
    and no worker session. Auto-clears stale alerts.

    Returns a small summary dict (``{"emitted": N, "cleared": M}``) for
    the sweep's by-outcome tally.
    """
    summary = {"emitted": 0, "cleared": 0}
    known = list(getattr(services, "known_projects", ()) or ())
    if not known:
        return summary
    window_names = _storage_closet_window_names(services)
    if window_names is None:
        # Can't enumerate sessions — bail rather than emit false positives.
        return summary
    for project in known:
        project_key = getattr(project, "key", None)
        if not project_key:
            continue
        n_queued = _count_queued_tasks_for_project(services, project)
        has_worker = _project_has_worker_session(project_key, window_names)
        if n_queued > 0 and not has_worker:
            _emit_worker_session_gap_alert(
                services, project=project_key, n_queued=n_queued,
            )
            summary["emitted"] += 1
        else:
            # Auto-clear: queue drained or worker came online.
            _clear_worker_session_gap_alert(services, project=project_key)
            summary["cleared"] += 1
    return summary


# #1070 — detection-only sweep for tasks whose per-task worker window
# died without the task being reverted to a recoverable state. The
# canonical trigger is ``pm reset --force`` killing the storage-closet
# tmux session mid-flight: the per-task ``task-<project>-<N>`` window
# disappears, but the task row still says ``in_progress`` / ``rework``.
# ``_recover_dead_claims`` (above) handles the auto-recovery path, but
# only for projects whose ``auto_claim`` flag is enabled AND only until
# the circuit breaker (#1012/#1014) trips. Outside those windows the
# task sits forever with a dead assignee, invisible from
# ``pm task list``. This sweep surfaces that gap as a warn-level alert
# so the operator notices and can intervene manually.

MISSING_TASK_WORKER_ALERT_TYPE = "missing_task_worker"


def _missing_task_worker_session_name(task_id: str) -> str:
    """Return the synthetic alert scope for a missing-worker alert.

    Keyed per task so the cockpit's per-session alert view groups the
    row under the affected task's id. ``upsert_alert`` dedupes on
    ``(scope, alert_type, status='open')`` so repeat ticks just
    refresh the existing row instead of stacking duplicates.
    """
    return f"missing_task_worker-{task_id}"


def _emit_missing_task_worker_alert(
    services: Any,
    *,
    task_id: str,
    project: str,
    task_number: int,
) -> None:
    """Raise (or refresh) a ``missing_task_worker`` alert for ``task_id``.

    Surfaces the invisible-failure case where a task is ``in_progress``
    or ``rework`` with a per-task worker role bound, but the
    corresponding ``task-<project>-<N>`` tmux window is missing from
    the storage-closet (worker died, ``pm reset --force`` blew it away,
    host reboot, etc.). DETECTION ONLY — recovery is the operator's
    job once the alert fires. The wording points at the manual
    recovery commands and the per-task log directory so the user can
    investigate the underlying death cause before re-claiming.
    """
    store = getattr(services, "msg_store", None) or getattr(
        services, "state_store", None,
    )
    if store is None:
        return
    scope = _missing_task_worker_session_name(task_id)
    log_hint = f"~/.pollypm/logs/task_{project}_{task_number}/"
    message = (
        f"Task {task_id} is in_progress/rework but its per-task worker "
        f"window (task-{project}-{task_number}) is missing from the "
        f"storage-closet — the worker died (likely via "
        f"`pm reset --force` or a crash) without the task being "
        f"reverted.\n"
        f"To recover: `pm task hold {task_id}` then "
        f"`pm task claim {task_id}` to spawn a fresh per-task worker.\n"
        f"To investigate the death cause first, check the task's tmux "
        f"logs at {log_hint}."
    )
    try:
        store.upsert_alert(
            scope,
            MISSING_TASK_WORKER_ALERT_TYPE,
            "warn",
            message,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment sweep: upsert_alert(missing_task_worker) "
            "failed for %s",
            scope, exc_info=True,
        )


def _clear_missing_task_worker_alert(services: Any, *, task_id: str) -> None:
    """Clear the open ``missing_task_worker`` alert for ``task_id``."""
    store = getattr(services, "msg_store", None) or getattr(
        services, "state_store", None,
    )
    if store is None:
        return
    scope = _missing_task_worker_session_name(task_id)
    try:
        store.clear_alert(scope, MISSING_TASK_WORKER_ALERT_TYPE)
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment sweep: clear_alert(missing_task_worker) "
            "failed for %s",
            scope, exc_info=True,
        )


def _list_active_worker_tasks(work: Any, project_key: str | None) -> list[Any]:
    """Return ``in_progress`` + ``rework`` tasks for ``project_key``.

    ``project_key=None`` means "list every active worker task in the DB"
    (used for the workspace-root pass which holds tasks for many
    projects). On any list_tasks error, returns whatever was collected
    so far — degrading partial-closed beats raising mid-sweep.
    """
    out: list[Any] = []
    for status in _ACTIVE_WORKER_STATUSES:
        try:
            if project_key is None:
                tasks = work.list_tasks(work_status=status)
            else:
                tasks = work.list_tasks(
                    project=project_key, work_status=status,
                )
        except Exception:  # noqa: BLE001
            logger.debug(
                "task_assignment sweep: list_tasks(%s) failed during "
                "missing_task_worker pass",
                status, exc_info=True,
            )
            continue
        out.extend(tasks)
    return out


def _sweep_missing_task_workers(services: Any) -> dict[str, int]:
    """Emit ``missing_task_worker`` alerts for stuck-with-no-worker tasks.

    #1070 — detection-only counterpart to ``_recover_dead_claims``.
    Walks every registered project's DB (and the workspace-root DB)
    for tasks whose ``work_status`` is ``in_progress`` / ``rework`` and
    whose ``roles`` dict declares a ``worker``. For each such task,
    asserts the corresponding ``task-<project>-<N>`` tmux window
    exists. If not, emits a warn-level alert pointing the operator at
    the manual recovery flow. Cleared automatically when the window
    reappears (operator manually re-claimed, supervisor respawned,
    etc.) or the task transitions out of the active-worker statuses.

    Returns ``{"emitted": N, "cleared": M, "considered": K}`` for the
    sweep's by-outcome tally. Cheap — one ``list_tasks`` per (project,
    status) plus one ``list_windows`` cached across the pass.
    """
    summary = {"emitted": 0, "cleared": 0, "considered": 0}
    window_names = _storage_closet_window_names(services)
    if window_names is None:
        # Can't enumerate sessions — bail rather than emit false positives.
        return summary

    def _check_task(project_key: str, task: Any) -> None:
        roles = getattr(task, "roles", {}) or {}
        if "worker" not in roles:
            return
        task_number = getattr(task, "task_number", None)
        if task_number is None:
            return
        try:
            task_n_int = int(task_number)
        except (TypeError, ValueError):
            return
        task_id = (
            getattr(task, "task_id", None)
            or f"{project_key}/{task_n_int}"
        )
        summary["considered"] += 1
        expected_window = f"task-{project_key}-{task_n_int}"
        if expected_window in window_names:
            # Worker is alive — clear any stale alert from a prior tick.
            _clear_missing_task_worker_alert(services, task_id=task_id)
            summary["cleared"] += 1
            return
        _emit_missing_task_worker_alert(
            services,
            task_id=task_id,
            project=project_key,
            task_number=task_n_int,
        )
        summary["emitted"] += 1

    # Pass 1: workspace-root DB (tasks created via ``pm work create``
    # land here even when their ``project`` field points at a
    # registered project). Filter by project key per registered project
    # so we only consider tasks bound to a known project — synthetic
    # workspace tasks without a project key have no per-task window
    # naming convention to check against.
    workspace_work = getattr(services, "work_service", None)
    known = list(getattr(services, "known_projects", ()) or ())
    if workspace_work is not None and known:
        for project in known:
            project_key = getattr(project, "key", None)
            if not project_key:
                continue
            for task in _list_active_worker_tasks(workspace_work, project_key):
                _check_task(project_key, task)

    # Pass 2: per-project DBs. Each gets its own short-lived connection,
    # closed inside the loop so we don't pile up file handles when many
    # projects are registered. Mirrors the open/close pattern used by
    # the main sweep body and ``_sweep_worker_session_gaps``.
    for project in known:
        project_key = getattr(project, "key", None)
        if not project_key:
            continue
        project_work = _open_project_work_service(project, services)
        if project_work is None:
            continue
        try:
            # Per-project DB — list_tasks with no project filter
            # returns all rows in that DB (which by definition belong
            # to this project).
            for task in _list_active_worker_tasks(project_work, None):
                _check_task(project_key, task)
        finally:
            _close_quietly(project_work)
    return summary


def _sweep_ghost_project_alerts(services: Any) -> int:
    """Clear ``no_session*`` alerts whose project is no longer registered.

    #1001 lifecycle bug: ``pm alerts`` kept surfacing
    ``no_session_for_assignment:<proj>/<n>`` and project-level
    ``worker-<proj>/no_session`` alerts for projects that were
    deregistered (or never existed). Each heartbeat tick re-fired
    them because the alert-fire path didn't validate the project
    against the live registry.

    The fire-path guard added alongside this helper stops *new* ghost
    alerts; this sweep pass clears the ones that have accumulated
    while the bug was live (and any that slip through future
    register/deregister races).

    No-op when the registry is empty — without a registry there's no
    signal for "project doesn't exist", and clearing in that case
    would prune real alerts in test / config-less runs.

    Returns the count of alerts cleared (mostly observability — the
    sweep handler folds this into its by_outcome tally).
    """
    store = services.msg_store or services.state_store
    if store is None:
        return 0
    known_keys = _known_project_keys(services)
    if not known_keys:
        return 0
    # Read open alerts. The unified messages Store exposes
    # ``query_messages``; the legacy ``StateStore`` (and several test
    # doubles) expose ``open_alerts`` which returns ``AlertRecord``s.
    # Fall back across both shapes so the sweep works regardless of
    # which backend the operator's config wired up.
    pairs: list[tuple[str, str]] = []
    query = getattr(store, "query_messages", None)
    if callable(query):
        try:
            for row in query(type="alert", state="open"):
                pairs.append(
                    (str(row.get("scope") or ""), str(row.get("sender") or "")),
                )
        except Exception:  # noqa: BLE001
            logger.debug(
                "task_assignment sweep: ghost-project query_messages failed",
                exc_info=True,
            )
            pairs = []
    if not pairs:
        list_open = getattr(store, "open_alerts", None)
        if callable(list_open):
            try:
                for alert in list_open():
                    pairs.append(
                        (
                            str(getattr(alert, "session_name", "") or ""),
                            str(getattr(alert, "alert_type", "") or ""),
                        ),
                    )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "task_assignment sweep: ghost-project open_alerts failed",
                    exc_info=True,
                )
                return 0
    cleared = 0
    for scope, alert_type in pairs:
        ghost_project: str | None = None
        if alert_type.startswith("no_session_for_assignment:"):
            # ``no_session_for_assignment:<project>/<task_number>``.
            tail = alert_type[len("no_session_for_assignment:"):]
            if "/" in tail:
                ghost_project = tail.rsplit("/", 1)[0]
        elif alert_type == "no_session":
            # Project-level alert — keyed by the candidate session name
            # we *would* have matched (``<role>-<project>`` /
            # ``<role>_<project>``). Strip the leading role prefix to
            # recover the project key.
            for sep in ("-", "_"):
                head, _, tail_part = scope.partition(sep)
                if tail_part and head in {"worker", "architect"}:
                    ghost_project = tail_part
                    break
        if ghost_project is None:
            continue
        if ghost_project in known_keys:
            continue
        try:
            store.clear_alert(scope, alert_type)
            cleared += 1
        except Exception:  # noqa: BLE001
            logger.debug(
                "task_assignment sweep: ghost-project clear_alert(%s, %s) failed",
                scope, alert_type, exc_info=True,
            )
    if cleared:
        logger.info(
            "task_assignment sweep: cleared %d ghost-project no_session alerts",
            cleared,
        )
    return cleared


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
    review_pending_tasks: set[tuple[str, int]] | None = None,
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
    # #938: when the caller didn't anchor the sweep to a specific project
    # (workspace-root pass), build a lookup of registered projects so we
    # can still apply the plan gate per task. Workspace-root DBs can hold
    # tasks belonging to any registered project; without this lookup the
    # sweep pings plan-gated workers before the per-project auto-claim
    # pass blocks them, producing the contradictory ``sent`` +
    # ``auto_claim_skipped_plan_missing`` outcome on the same task.
    known_by_key: dict[str, Any] = {}
    if project is None and project_path is None:
        for known in getattr(services, "known_projects", ()) or ():
            known_key = getattr(known, "key", None)
            if known_key:
                known_by_key[known_key] = known
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
            # #1053: surface review-state tasks via a ``review_pending``
            # alert so ``pm alerts`` shows the user what's awaiting their
            # approval. Emission is independent of the assignment-notify
            # path below — even if no machine actor is bound, a task in
            # ``review`` is by definition work-for-the-human and should
            # be visible. Tracked so the post-sweep clearing pass can
            # close alerts whose task transitioned out of ``review``.
            if (
                review_pending_tasks is not None
                and status == WorkStatus.REVIEW.value
            ):
                review_project = getattr(task, "project", None)
                review_task_n = getattr(task, "task_number", None)
                if review_project and review_task_n is not None:
                    review_pending_tasks.add(
                        (str(review_project), int(review_task_n)),
                    )
                    _emit_review_pending_alert(
                        services,
                        project=str(review_project),
                        task=task,
                    )

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
            # target session restarts later. We apply the gate for
            # per-project sweeps (``project_path`` supplied) and, on
            # the workspace-root pass, for any task whose ``project``
            # key matches a registered project — otherwise #938 lets
            # us ping plan-gated tasks before auto-claim's gate fires.
            gate_path = project_path
            gate_enforce = enforce_plan
            if (
                gate_path is None
                and known_by_key
                and event.project in known_by_key
            ):
                anchor = known_by_key[event.project]
                anchor_path = getattr(anchor, "path", None)
                if anchor_path is not None:
                    gate_path = anchor_path
                    anchor_enforce = getattr(anchor, "enforce_plan", None)
                    gate_enforce = (
                        anchor_enforce
                        if anchor_enforce is not None
                        else global_enforce
                    )
            if (
                gate_enforce
                and gate_path is not None
                and status == WorkStatus.QUEUED.value
                and not task_bypasses_plan_gate(task)
            ):
                if not _plan_gate_allows(
                    event.project,
                    gate_path,
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
            # #922/#952: when kickoff hasn't been delivered yet, bypass
            # stale normal notification rows so a poisoned pre-pane send
            # can't suppress the first real push. Still claim a short,
            # separate forced-kickoff dedupe slot so concurrent sweep ticks
            # cannot stack duplicate Resume-work pings during boot.
            effective_throttle = 0 if kickoff_pending else throttle_override
            result = notify(
                event,
                services=services,
                throttle_seconds=effective_throttle,
                atomic_dedupe_seconds=(
                    RECENT_SWEEPER_PING_SECONDS if kickoff_pending else None
                ),
                dedupe_scope="forced_kickoff" if kickoff_pending else "normal",
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


def _consecutive_abandonments_at_active_node(task: Any) -> int:
    """Return the count of consecutive ``ABANDONED`` executions at the
    task's current node, walking backwards from the most recent visit.

    #1012 — used by the circuit breaker. Stops counting at the first
    non-ABANDONED status so a successful run between two failures
    resets the streak (matching the user's mental model of "how many
    spawn attempts in a row have failed since the last useful turn").
    Walks across nodes are ignored because a failing per-task spawn
    bounces against a single ``build`` (or whichever worker) node;
    tracking only that node avoids confusing rejection bounces (which
    visit the build node from the review node) with spawn failures.
    """
    current_node = getattr(task, "current_node_id", None)
    if not current_node:
        return 0
    executions = list(getattr(task, "executions", []) or [])
    # Sort newest-first by visit so we can walk backwards.
    try:
        executions.sort(
            key=lambda e: int(getattr(e, "visit", 0) or 0),
            reverse=True,
        )
    except Exception:  # noqa: BLE001
        return 0
    streak = 0
    abandoned_value = ExecutionStatus.ABANDONED.value
    for execution in executions:
        if getattr(execution, "node_id", None) != current_node:
            continue
        status = getattr(execution, "status", None)
        status_value = getattr(status, "value", status)
        if status_value == abandoned_value:
            streak += 1
            continue
        if status_value == ExecutionStatus.ACTIVE.value:
            # The currently-active execution has not finished yet —
            # don't count it but also don't reset the streak. The
            # caller is invoked before ``release_stale_claim`` on the
            # tick that's about to abandon this row, so the streak we
            # care about is the one from prior visits.
            continue
        # Any completed visit (success / blocked / etc.) resets the
        # streak — the worker did real work between failures.
        break
    return streak


def _abandonments_within_window(
    task: Any,
    *,
    window_seconds: int = RATE_BREAKER_WINDOW_SECONDS,
    now: datetime | None = None,
) -> int:
    """Return the count of ABANDONED executions inside the rate window.

    #1014 (Bug A) — companion to
    :func:`_consecutive_abandonments_at_active_node`. The streak
    counter only catches consecutive abandons at the *current* node;
    a reject bounce that walks the task back through the review node
    silently resets it. The rate counter is node-agnostic — any
    abandoned execution within ``window_seconds`` counts — so a
    spinning loop that ping-pongs between build and review can't slip
    past the breaker.

    Falls back to 0 on any parse / sort error so the breaker degrades
    closed (i.e. doesn't accidentally trip on a malformed row). The
    streak breaker stays as the primary guard; this one only adds a
    second tripwire that's robust to node transitions.
    """
    executions = list(getattr(task, "executions", []) or [])
    if not executions:
        return 0
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=window_seconds)
    abandoned_value = ExecutionStatus.ABANDONED.value
    count = 0
    for execution in executions:
        status = getattr(execution, "status", None)
        status_value = getattr(status, "value", status)
        if status_value != abandoned_value:
            continue
        ts_raw = (
            getattr(execution, "completed_at", None)
            or getattr(execution, "started_at", None)
        )
        if ts_raw is None:
            continue
        ts = _parse_execution_timestamp(ts_raw)
        if ts is None:
            continue
        if ts >= cutoff:
            count += 1
    return count


def _parse_execution_timestamp(value: Any) -> datetime | None:
    """Coerce an execution-row timestamp into a tz-aware datetime."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str) and value:
        try:
            ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    return None


def _emit_spawn_failed_persistent_alert(
    services: Any,
    *,
    project: str,
    task_id: str,
    streak: int,
) -> None:
    """Escalate the project's ``worker/no_session`` to error severity.

    #1012 — fires once the abandonment streak has crossed
    :data:`MAX_CONSECUTIVE_ABANDONMENTS`. The alert is keyed by the
    canonical worker-session candidate (``worker-<project>``) so the
    cockpit's per-session alert view groups it next to the warn-level
    ``no_session`` row that's still firing in parallel until the next
    sweep clears it. ``upsert_alert`` dedupes on ``(scope,
    alert_type, status='open')`` so repeat ticks just refresh the row
    rather than stacking duplicates.
    """
    store = getattr(services, "msg_store", None) or getattr(services, "state_store", None)
    if store is None:
        return
    candidates = role_candidate_names("worker", project)
    expected_name = candidates[0] if candidates else f"worker-{project}"
    message = (
        f"Auto-spawn for task {task_id} has failed {streak} times in a row "
        "— manual intervention required. The per-task worker session never "
        "materialised after the auto-claim sweep tried to spawn it. "
        f"Try `pm task claim {task_id}` from a clean shell, or check the "
        "supervisor logs for the underlying launch failure."
    )
    try:
        store.upsert_alert(
            expected_name,
            SPAWN_FAILED_PERSISTENT_ALERT_TYPE,
            "error",
            message,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_auto_claim: spawn_failed_persistent upsert_alert failed for %s",
            expected_name, exc_info=True,
        )


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

    #1012 — circuit breaker: when the same task has accumulated
    :data:`MAX_CONSECUTIVE_ABANDONMENTS` consecutive abandoned
    executions on its active node without producing a working session,
    we stop releasing fresh visits. The task stays ``in_progress`` and
    a ``spawn_failed_persistent`` (error) alert is raised so the user
    sees an actionable blocker instead of a silent forever-loop that
    only manifests as a slow-growing execution history.
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

        # #1012 / #1014 (Bug A) — circuit breaker. Refuse to release the
        # claim once either tripwire fires:
        #
        # 1. ``streak >= MAX_CONSECUTIVE_ABANDONMENTS`` — the original
        #    #1012 guard. Strict consecutive count at the current node.
        # 2. ``rate_count >= RATE_BREAKER_MAX_ABANDONMENTS`` — added in
        #    #1014. Counts ABANDONED visits across all nodes within
        #    ``RATE_BREAKER_WINDOW_SECONDS``. Catches the case where a
        #    reject-bounce silently resets the streak counter (or where
        #    the streak counter is unexpectedly zeroed by a stray
        #    completed visit between failures).
        #
        # Either tripwire produces the same end state: the task stays
        # ``in_progress`` with a dead assignee, and a
        # ``spawn_failed_persistent`` error alert escalates the project's
        # silent grow-the-history loop into a visible blocker.
        streak = _consecutive_abandonments_at_active_node(task)
        rate_count = _abandonments_within_window(task)
        streak_tripped = streak >= MAX_CONSECUTIVE_ABANDONMENTS
        rate_tripped = rate_count >= RATE_BREAKER_MAX_ABANDONMENTS
        if streak_tripped or rate_tripped:
            by_outcome["auto_claim_circuit_breaker"] = (
                by_outcome.get("auto_claim_circuit_breaker", 0) + 1
            )
            _emit_spawn_failed_persistent_alert(
                services,
                project=project_key,
                task_id=task_id,
                streak=max(streak, rate_count),
            )
            logger.warning(
                "task_auto_claim: circuit-breaker tripped for %s "
                "(streak=%d at node %r, rate=%d in last %ds); refusing "
                "to release stale claim until the underlying spawn "
                "failure is resolved (#1012/#1014)",
                task_id, streak, getattr(task, "current_node_id", None),
                rate_count, RATE_BREAKER_WINDOW_SECONDS,
            )
            continue

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


def _precompute_plan_missing_projects(services: Any) -> dict[str, str | None]:
    """Return a mapping of plan-gated project keys → an example queued task_id.

    #1524 — computed up front from the canonical workspace DB so the
    sweep handler's emit/clear decisions are deterministic across the
    workspace pass + per-project pass. Without this precompute the set
    was populated as a side effect of the auto-claim loop, which only
    runs for projects with ``auto_claim`` enabled and a queued
    worker-role task — so other plan-gated projects appeared and
    disappeared from the set tick-to-tick depending on which loop
    happened to traverse them, producing the banner flash.

    Falls back to an empty mapping on any error so a transient I/O
    hiccup doesn't accidentally clear every plan_missing alert.
    """
    try:
        global_enforce = bool(getattr(services, "enforce_plan", True))
        if not global_enforce:
            return {}
        known = list(getattr(services, "known_projects", ()) or ())
        if not known:
            return {}
        plan_dir = getattr(services, "plan_dir", "docs/plan") or "docs/plan"

        def _opener(project: Any) -> Any | None:
            return _open_workspace_project_work_service(project, services)

        return compute_plan_missing_projects(
            known,
            work_service_for_project=_opener,
            plan_dir=plan_dir,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment sweep: plan-missing precompute failed",
            exc_info=True,
        )
        return {}


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
        from pollypm.work import create_work_service

        project_root = Path(project_path)
        svc = create_work_service(db_path=db_path, project_path=project_root)
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
        from pollypm.work import create_work_service

        svc = create_work_service(db_path=db_path, project_path=Path(project_path))
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
    # #1069 — release services.state_store at the end of every code
    # path. ``load_runtime_services`` opens a fresh ``StateStore`` on
    # every call (resolver.py L96); without this finally the @every
    # 30s sweep cadence leaked one sqlite connection per tick (≈4
    # fds/min once WAL handles are counted) under live activity.
    # ``services.work_service`` is closed by the existing
    # ``_close_quietly(workspace_work)`` site below; ``services.close()``
    # only re-runs the (idempotent) state_store close after that, so
    # a double-close is benign — we still call it for symmetry.
    try:
        return _task_assignment_sweep_body(
            services=services,
            payload=payload,
            config_path=config_path,
        )
    finally:
        try:
            services.close()
        except Exception:  # noqa: BLE001
            logger.debug(
                "task_assignment sweep: services.close raised", exc_info=True,
            )


def _task_assignment_sweep_body(
    *,
    services: Any,
    payload: dict[str, Any],
    config_path: Path | None,
) -> dict[str, Any]:
    """Inner body of :func:`task_assignment_sweep_handler`.

    Split out so the outer handler can guarantee
    :meth:`_RuntimeServices.close` runs on every return path (#1069).
    """
    # The sweeper uses a shorter throttle so pre-existing queued tasks
    # get re-pinged every 5 min if they stay unclaimed — that's the
    # "session came online late" recovery path from the spec.
    throttle_override = int(payload.get("throttle_seconds", SWEEPER_COOLDOWN_SECONDS))
    if throttle_override < 1:
        throttle_override = SWEEPER_COOLDOWN_SECONDS

    totals: dict[str, Any] = {"considered": 0, "by_outcome": {}}
    alerted_pairs: set[tuple[str, str]] = set()
    # #1524 — pre-compute the set of projects that are plan-gated this
    # tick so the emit/clear decision is deterministic across the
    # workspace pass + per-project pass. Pre-#1524 this set was derived
    # as a side effect of the auto-claim loop, so a project would only
    # land in it on ticks that happened to traverse the right path.
    # After the per-project legacy DB was drained (post-#1519) the
    # per-project fallback then cleared the alert on every other tick,
    # producing the "Waiting on you / Queued" banner flash described in
    # the issue. Computing once up front means the clear paths skip
    # plan-gated projects every tick, regardless of which loop runs.
    precomputed_missing = _precompute_plan_missing_projects(services)
    plan_missing_projects: set[str] = set(precomputed_missing.keys())
    plan_decisions: dict[str, bool] = {}
    # Seed the per-tick gate cache with the precomputed answers so the
    # workspace + per-project sweeps reuse the deterministic decision
    # instead of recomputing (and potentially diverging) per task.
    for missing_project in plan_missing_projects:
        plan_decisions[missing_project] = False
    # #1053: ``(project, task_number)`` pairs for tasks observed in
    # ``review`` state during this sweep cycle. After the sweep, any
    # open ``review_pending`` alert whose key isn't in this set is
    # stale (task approved / rejected / cancelled) and gets cleared.
    review_pending_tasks: set[tuple[str, int]] = set()
    projects_scanned = 0
    projects_skipped = 0

    # #1524 — refresh the ``plan_missing`` alert row up front for every
    # project in the precomputed set. This guarantees the alert stays
    # open across consecutive ticks even when no queued task is reached
    # in the per-project sweep body (e.g. legacy per-project DB has
    # been drained but the canonical DB still has blocked work). The
    # downstream per-task emit paths still fire to refresh the row when
    # they encounter the first blocked task; the up-front refresh just
    # ensures the row exists before any clear path could run.
    for missing_project, example_task_id in precomputed_missing.items():
        _emit_plan_missing_alert(
            services,
            project=missing_project,
            example_task_id=example_task_id or missing_project,
        )

    # #1001: clear stale ``no_session*`` alerts whose project key isn't
    # in the live registry. Run before the per-project sweeps so any
    # row left over from a deregistered project doesn't survive the
    # tick. The fire-path guard in ``_emit_no_session_alert`` /
    # ``_escalate_no_session`` keeps new ghost alerts from being raised
    # in the same pass.
    ghost_cleared = _sweep_ghost_project_alerts(services)
    if ghost_cleared:
        totals["by_outcome"]["ghost_project_alerts_cleared"] = ghost_cleared

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
                review_pending_tasks=review_pending_tasks,
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
                review_pending_tasks=review_pending_tasks,
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

    # #1053: clear stale ``review_pending`` alerts. Walk every open
    # alert with ``alert_type == 'review_pending'`` and close any whose
    # ``(project, task_number)`` key wasn't observed in the review-state
    # tracking set above. Tasks that were approved, rejected, or
    # cancelled since the previous sweep tick are no longer in
    # ``review`` and shouldn't keep nagging the user.
    review_pending_cleared = _sweep_stale_review_pending_alerts(
        services, review_pending_tasks,
    )

    # #1005: after the sweep has refreshed the open alert set, walk any
    # ``<role>/no_session`` alerts and attempt auto-recovery
    # (``pm worker-start --role <role> <project>``). The helper bounds
    # retries, applies an exponential backoff per (role, project), and
    # escalates to ``<role>/no_session_spawn_failed`` once attempts
    # exhaust — mirroring the heartbeat's ``recovery_limit`` pattern.
    spawn_summary: dict[str, int] = {}
    try:
        from pollypm.recovery.no_session_spawn import (
            auto_recover_no_session_alerts,
            summarize_decisions,
        )

        decisions = auto_recover_no_session_alerts(
            services,
            config_path=config_path,
        )
        spawn_summary = summarize_decisions(decisions)
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment sweep: no_session auto-recovery failed",
            exc_info=True,
        )

    # #1054: surface projects that have queued tasks but no
    # ``worker_<project>`` (or ``worker-<project>``) tmux session — the
    # invisible-failure case where queued work sits forever because no
    # worker exists to claim it. Sister of #1053.
    worker_gap_summary = {"emitted": 0, "cleared": 0}
    try:
        worker_gap_summary = _sweep_worker_session_gaps(services)
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment sweep: worker_session_gap pass failed",
            exc_info=True,
        )

    # #1070: surface tasks that are ``in_progress`` / ``rework`` with a
    # per-task worker role bound but whose ``task-<project>-<N>`` tmux
    # window is missing from the storage-closet (worker died, daemon
    # restart blew it away, etc.). Detection only — recovery is the
    # operator's job once the alert fires. Independent of
    # ``auto_claim`` so projects with auto-claim disabled (or with
    # tripped circuit breakers) still surface the gap.
    missing_worker_summary = {"emitted": 0, "cleared": 0, "considered": 0}
    try:
        missing_worker_summary = _sweep_missing_task_workers(services)
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment sweep: missing_task_worker pass failed",
            exc_info=True,
        )

    return {
        "outcome": "swept",
        "considered": totals["considered"],
        "by_outcome": totals["by_outcome"],
        "projects_scanned": projects_scanned,
        "projects_skipped": projects_skipped,
        "no_session_alerts": len(alerted_pairs),
        "plan_missing_alerts": len(plan_missing_projects),
        "review_pending_alerts": len(review_pending_tasks),
        "review_pending_cleared": review_pending_cleared,
        "no_session_auto_recovery": spawn_summary,
        "worker_session_gap": worker_gap_summary,
        "missing_task_worker": missing_worker_summary,
    }
