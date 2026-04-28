"""Plugin-side helpers that wire the work-assignment bus to the rail's
session service + state store.

The real business logic (event shape, role → name convention, message
text) lives in ``pollypm.work.task_assignment``. This module only
glues the plugin to the runtime services — config loading, session
service instantiation, dedupe / escalate decisions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pollypm.work.task_assignment import (
    SessionRoleIndex,
    TaskAssignmentEvent,
    format_ping_for_role,
)

logger = logging.getLogger(__name__)


# 30-minute throttle on (session, task) pings — the spec's dedupe window.
DEDUPE_WINDOW_SECONDS = 30 * 60

# Sweeper's re-enqueue cooldown — we re-notify a stale sitter every 5 min
# at most even when the sweep cadence is 30s.
SWEEPER_COOLDOWN_SECONDS = 5 * 60


@dataclass(slots=True)
class _RuntimeServices:
    """Container for the services a notify/sweep invocation needs.

    ``known_projects`` — the registered ``[projects.*]`` map from
    config, used by the sweeper to fan out across per-project work-
    service DBs (#259). Empty tuple when no projects are registered or
    config didn't load; the sweep then only scans the workspace-root DB.

    ``enforce_plan`` / ``plan_dir`` — ``[planner]`` config knobs read at
    service-load time so the sweep handler can evaluate the plan-
    presence gate (#273) without re-parsing config on every task.
    ``enforce_plan`` defaults to True (gate active); ``plan_dir``
    defaults to ``"docs/plan"``.
    """

    session_service: Any | None
    state_store: Any | None
    work_service: Any | None
    project_root: Path
    # #349: unified-messages Store handle alongside the legacy
    # ``state_store`` so writer sites (upsert_alert / record_event /
    # clear_alert) can flip onto ``messages`` while readers that still
    # need StateStore-specific APIs (raw ``execute``, ``was_notified_within``,
    # ``record_notification``) keep working.
    msg_store: Any | None = None
    config: Any | None = None
    storage_closet_name: str = "pollypm-storage-closet"
    known_projects: tuple[Any, ...] = field(default_factory=tuple)
    enforce_plan: bool = True
    plan_dir: str = "docs/plan"
    # #768 auto-claim — global default + per-project caps
    auto_claim: bool = True
    max_concurrent_per_project: int = 2


def load_runtime_services(
    *,
    config_path: Path | None = None,
) -> _RuntimeServices:
    """Resolve the session service + state store + work service from config.

    Returns a container with ``None`` slots when a particular piece can't
    be built (e.g. tests running without a config). Callers should treat
    missing services as a soft skip.
    """
    from pollypm.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path

    resolved_path = config_path or resolve_config_path(DEFAULT_CONFIG_PATH)
    if not resolved_path or not resolved_path.exists():
        return _RuntimeServices(
            session_service=None,
            state_store=None,
            work_service=None,
            project_root=Path.cwd(),
            known_projects=(),
            msg_store=None,
        )
    config = load_config(resolved_path)

    from pollypm.storage.state import StateStore

    store = StateStore(config.project.state_db)

    # #349: unified-messages Store handle. Falls back to ``None`` if the
    # backend can't be resolved so the caller still has ``state_store``
    # to fall back on for the legacy tables.
    msg_store: Any | None
    try:
        from pollypm.store.registry import get_store

        msg_store = get_store(config)
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment_notify: unified Store unavailable",
            exc_info=True,
        )
        msg_store = None

    # Session service — try the tmux default; any failure means we have
    # no way to ping, so the caller escalates to a user-inbox alert.
    session_service: Any | None
    try:
        from pollypm.session_services.tmux import TmuxSessionService

        session_service = TmuxSessionService(config=config, store=store)
    except Exception:  # noqa: BLE001
        logger.debug("task_assignment_notify: session service unavailable", exc_info=True)
        session_service = None

    # Work service — needed by the sweeper to enumerate queued/review
    # tasks and by the resolver to count in-progress claims for
    # disambiguation. Match the CLI's default workspace-root DB
    # convention instead of deriving from the global config directory.
    project_root = Path(
        getattr(config.project, "workspace_root", None) or config.project.root_dir
    )
    work_service: Any | None
    try:
        from pollypm.work.sqlite_service import SQLiteWorkService

        db_path = project_root / ".pollypm" / "state.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        work_service = SQLiteWorkService(db_path=db_path, project_path=project_root)
    except Exception:  # noqa: BLE001
        logger.debug("task_assignment_notify: work service unavailable", exc_info=True)
        work_service = None

    # #259: snapshot registered projects so the sweeper can open each
    # per-project DB on its own tick. We store the KnownProject objects
    # directly — the sweeper just needs ``.key`` and ``.path``.
    known_projects = tuple(config.projects.values())

    return _RuntimeServices(
        session_service=session_service,
        state_store=store,
        work_service=work_service,
        project_root=project_root,
        config=config,
        storage_closet_name=f"{config.project.tmux_session}-storage-closet",
        known_projects=known_projects,
        enforce_plan=config.planner.enforce_plan,
        plan_dir=config.planner.plan_dir,
        msg_store=msg_store,
        auto_claim=config.planner.auto_claim,
        max_concurrent_per_project=config.planner.max_concurrent_per_project,
    )


# ---------------------------------------------------------------------------
# Notify primitive — used by both the event listener and the sweeper.
# ---------------------------------------------------------------------------


def notify(
    event: TaskAssignmentEvent,
    *,
    services: _RuntimeServices,
    throttle_seconds: int = DEDUPE_WINDOW_SECONDS,
) -> dict[str, Any]:
    """Resolve + dedupe + send a single assignment ping.

    Returns a small dict describing the outcome so handlers can include
    it in their job-result payload. Never raises — every error path is
    classified (``"no_session"``, ``"deduped"``, ``"send_failed"``,
    ``"no_session_service"``).
    """
    session_svc = services.session_service
    store = services.state_store
    msg_store = services.msg_store

    if session_svc is None:
        _escalate_no_session_service(event, msg_store or store)
        return {"outcome": "no_session_service", "task_id": event.task_id}

    index = SessionRoleIndex(session_svc, work_service=services.work_service)
    # #921: pass ``task_number`` so the resolver picks up post-#919
    # per-task worker windows (``task-<project>-<N>``) in addition to
    # the legacy ``worker-<project>`` / ``worker_<project>`` long-lived
    # sessions.
    handle = index.resolve(
        event.actor_type,
        event.actor_name,
        event.project,
        task_number=event.task_number,
    )

    if handle is None:
        _escalate_no_session(event, msg_store or store)
        return {
            "outcome": "no_session",
            "task_id": event.task_id,
            "actor_type": event.actor_type.value,
            "actor_name": event.actor_name,
        }

    target_name = getattr(handle, "name", "")

    # #279: key the dedupe on ``(session, task, execution_version)``.
    # A rejection that bounces the task back to an earlier node opens a
    # fresh ``work_node_executions.visit`` — that shows up here as a new
    # ``execution_version`` and correctly lets the retry ping through
    # even inside the 30-minute window that originally throttled the
    # first ping at ``visit=1``. Events with no version (``0``) still
    # dedupe against pre-migration rows (column DEFAULT 0), preserving
    # the original throttle semantics across the upgrade.
    execution_version = int(getattr(event, "execution_version", 0) or 0)

    message = format_ping_for_role(event)

    # #952: dedupe the slot atomically BEFORE the send, not after.
    # The legacy flow was [check was_notified_within → send →
    # record_notification], which left a TOCTOU window: concurrent
    # sweep ticks all saw "not yet sent" and each fired the same
    # ``Resume work`` ping before any of them committed the row.
    # ``claim_notification_slot`` checks + inserts under the connection
    # lock in one atomic step. The first caller wins (returns a rowid);
    # losers see the placeholder row and return None → ``deduped``. We
    # then update the row's ``delivery_status`` once the send finishes.
    notification_id: int | None = None
    can_claim = (
        store is not None
        and throttle_seconds > 0
        and hasattr(store, "claim_notification_slot")
    )
    if can_claim:
        try:
            notification_id = store.claim_notification_slot(
                session_name=target_name,
                task_id=event.task_id,
                window_seconds=throttle_seconds,
                execution_version=execution_version,
                project=event.project,
                message=message,
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "task_assignment_notify: claim_notification_slot failed for %s",
                event.task_id, exc_info=True,
            )
            notification_id = None
        if notification_id is None:
            return {
                "outcome": "deduped",
                "task_id": event.task_id,
                "session": target_name,
                "execution_version": execution_version,
            }
    elif store is not None and throttle_seconds > 0:
        # Legacy fallback for stores that don't expose the atomic
        # claim helper (test doubles, custom backends). Preserves the
        # old behaviour — racy on concurrent sweeps but correct for
        # serial callers.
        try:
            if store.was_notified_within(
                target_name,
                event.task_id,
                throttle_seconds,
                execution_version,
            ):
                return {
                    "outcome": "deduped",
                    "task_id": event.task_id,
                    "session": target_name,
                    "execution_version": execution_version,
                }
        except Exception:  # noqa: BLE001
            logger.debug(
                "task_assignment_notify: dedupe check failed for %s",
                event.task_id, exc_info=True,
            )

    # Clear any prior "no session" alert for this task — the recipient
    # is back online. #349: writers land in ``messages`` via the Store.
    if msg_store is not None:
        try:
            msg_store.clear_alert("task_assignment", _alert_type_for(event))
        except Exception:  # noqa: BLE001
            pass
        # #921: also clear the sweep-level ``(worker-<project>, no_session)``
        # alert raised by ``_emit_no_session_alert``. That alert is
        # keyed by the candidate session name we *would* expect, not by
        # the actual matched name (which for a per-task session is
        # ``task-<project>-<N>``), so we walk the role candidates.
        from pollypm.work.models import ActorType as _ActorType

        if event.actor_type is _ActorType.ROLE:
            from pollypm.work.task_assignment import role_candidate_names

            for candidate in role_candidate_names(
                event.actor_name, event.project,
            ):
                try:
                    msg_store.clear_alert(candidate, "no_session")
                except Exception:  # noqa: BLE001
                    pass

    try:
        session_svc.send(target_name, message)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "task_assignment_notify: send to %s failed: %s", target_name, exc,
        )
        if store is not None:
            failure_status = f"failed: {exc}"[:200]
            if notification_id is not None:
                try:
                    store.update_notification_status(
                        notification_id,
                        delivery_status=failure_status,
                    )
                except Exception:  # noqa: BLE001
                    pass
            else:
                try:
                    store.record_notification(
                        session_name=target_name,
                        task_id=event.task_id,
                        project=event.project,
                        message=message,
                        delivery_status=failure_status,
                        execution_version=execution_version,
                    )
                except Exception:  # noqa: BLE001
                    pass
        return {
            "outcome": "send_failed",
            "task_id": event.task_id,
            "session": target_name,
            "error": str(exc),
            "execution_version": execution_version,
        }

    if store is not None:
        if notification_id is not None:
            try:
                store.update_notification_status(
                    notification_id,
                    delivery_status="sent",
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "task_assignment_notify: update_notification_status failed for %s",
                    event.task_id, exc_info=True,
                )
        else:
            try:
                store.record_notification(
                    session_name=target_name,
                    task_id=event.task_id,
                    project=event.project,
                    message=message,
                    delivery_status="sent",
                    execution_version=execution_version,
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "task_assignment_notify: record_notification failed for %s",
                    event.task_id, exc_info=True,
                )

    # #923: ``notify()`` deliberately does NOT stamp ``kickoff_sent_at``
    # any more. The transition-time call site (claim → in_process listener)
    # races the per-task pane bootstrap: the message can be sent into a
    # still-loading pane and silently lost while the stamp lands as if
    # delivery succeeded. The sweep handler is now the sole writer to the
    # marker — it only stamps after observing a successful send against
    # an actually-resolvable session, so a lost transition-time push gets
    # re-delivered on the next sweep tick. See ``handlers/sweep.py``.

    return {
        "outcome": "sent",
        "task_id": event.task_id,
        "session": target_name,
        "execution_version": execution_version,
    }


def _is_worker_kickoff_event(event: TaskAssignmentEvent) -> bool:
    """Return True when this event represents a worker-role kickoff.

    The kickoff_sent stamp (#922) is meaningful only for worker pings
    landing on per-task ``task-<project>-<N>`` panes. Reviewer / operator
    / agent pings already work via the long-lived shared sessions, so
    they don't need the force-push gate.
    """
    from pollypm.work.models import ActorType

    if event.actor_type is not ActorType.ROLE:
        return False
    if (event.actor_name or "").strip().lower() != "worker":
        return False
    # Review nodes are reviewer territory; the kickoff race only bites
    # the work / rework path on a freshly-spawned per-task pane.
    if event.current_node_kind == "review":
        return False
    return True


def _mark_kickoff_delivered(
    event: TaskAssignmentEvent,
    work_service: Any | None,
) -> None:
    """Best-effort: stamp the active execution row's kickoff_sent_at.

    #923: this is now called *only* from the sweep handler, after a
    confirmed-target send has succeeded. Stamping from the transition-
    time ``notify()`` was unsafe because the per-task pane is often
    still bootstrapping at claim time — the keystrokes land in a
    still-loading shell and are lost while the stamp records as if
    delivery succeeded.

    Silently no-ops when the work service doesn't expose
    ``mark_kickoff_sent`` (test doubles), when the event isn't a worker
    kickoff, or when the service raises — a missing stamp at worst lets
    the next sweep tick re-fire once.
    """
    if work_service is None:
        return
    if not _is_worker_kickoff_event(event):
        return
    marker = getattr(work_service, "mark_kickoff_sent", None)
    if not callable(marker):
        return
    try:
        marker(
            event.project,
            event.task_number,
            event.current_node,
            event.execution_version or None,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment_notify: mark_kickoff_sent failed for %s",
            event.task_id, exc_info=True,
        )


# ---------------------------------------------------------------------------
# Escalation helpers
# ---------------------------------------------------------------------------


def _alert_type_for(event: TaskAssignmentEvent) -> str:
    """Return the alert_type used to dedupe no-session alerts per task."""
    return f"no_session_for_assignment:{event.task_id}"


def clear_alerts_for_cancelled_task(
    *,
    task_id: str,
    project: str,
    role_names: tuple[str, ...] = ("worker",),
    has_other_active_for_role: dict[str, bool] | None = None,
    config_path: Path | None = None,
    store: Any | None = None,
) -> dict[str, Any]:
    """Clear no_session alerts that referenced a now-cancelled task.

    Two-tier cleanup matching the two alert families the sweep / notify
    path raises:

    * Per-task ``no_session_for_assignment:<task_id>`` — unambiguous.
      Always cleared, since the task is terminal.
    * Per-project ``(worker-<project>, no_session)`` — only cleared
      when no other active task on the project still routes to that
      role. ``has_other_active_for_role`` maps a role name to True
      when the project has another active task for that role; the
      caller computes this against the work service so we don't
      reach back across the work / plugins layering.

    Best-effort: any error opening services / clearing alerts is
    swallowed. The next sweep tick will re-emit if the task somehow
    re-enters an active state, and the existing #919 stale-alert
    sweep guard remains intact.

    ``store`` lets the caller inject the alert store directly — the
    work-service cancel path does this so we don't open a second
    SQLite connection against the same DB while the caller's
    transaction is still in flight. When ``store`` is ``None`` we
    resolve runtime services from config and use the resulting
    msg_store/state_store handle.
    """
    cleared_per_task = False
    cleared_project: list[str] = []
    owns_work_service = False
    services = None
    if store is None:
        services = load_runtime_services(config_path=config_path)
        owns_work_service = True
        store = services.msg_store or services.state_store
    if store is None:
        # Best-effort: nothing to do without a store. Still close any
        # incidental work-service connection the resolver opened.
        if owns_work_service and services is not None:
            work_closer = getattr(services.work_service, "close", None)
            if callable(work_closer):
                try:
                    work_closer()
                except Exception:  # noqa: BLE001
                    pass
        return {
            "cleared_per_task": cleared_per_task,
            "cleared_project": cleared_project,
        }
    try:
        store.clear_alert(
            "task_assignment", f"no_session_for_assignment:{task_id}",
        )
        cleared_per_task = True
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_assignment_notify: clear_alert(no_session_for_assignment) "
            "failed for %s", task_id, exc_info=True,
        )
    # Lazy import — keep this module's import graph independent of
    # ``pollypm.work.task_assignment`` for the simple alert-clear
    # path so test doubles can mock ``role_candidate_names``.
    from pollypm.work.task_assignment import role_candidate_names

    active_map = has_other_active_for_role or {}
    for role in role_names:
        # Project-level alert: skip if the project still has another
        # active task routed to this role. The per-task alert above is
        # unambiguous; the project-level one is a single row that must
        # remain visible while *any* task on that role is blocked.
        if active_map.get(role, False):
            continue
        for candidate in role_candidate_names(role, project):
            try:
                store.clear_alert(candidate, "no_session")
                cleared_project.append(candidate)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "task_assignment_notify: clear_alert(no_session) "
                    "failed for %s", candidate, exc_info=True,
                )
    # Close the work-service connection we incidentally opened via
    # ``load_runtime_services`` — the cancel call site owns its own
    # connection and we mustn't leak ours.
    if owns_work_service and services is not None:
        work_closer = getattr(services.work_service, "close", None)
        if callable(work_closer):
            try:
                work_closer()
            except Exception:  # noqa: BLE001
                pass
    return {
        "cleared_per_task": cleared_per_task,
        "cleared_project": cleared_project,
    }


def _escalate_no_session(event: TaskAssignmentEvent, store: Any | None) -> None:
    """Raise (or refresh) a user-inbox alert when no session matches."""
    if store is None:
        return
    # #760 — action-forward single-line copy. Names the actor in plain
    # English, picks the role-correct worker-start hint instead of the
    # old always-architect default.
    from pollypm.work.models import ActorType

    if event.actor_type is ActorType.ROLE:
        actor_display = f"the {event.actor_name} role"
        if event.actor_name == "architect":
            try_hint = f"Try: pm worker-start --role architect {event.project}"
        elif event.actor_name == "worker":
            # pm worker-start --role=worker is deprecated — pm task
            # claim spawns a per-task worker, which is the preferred
            # path. See pm worker-start --help.
            try_hint = f"Try: pm task claim {event.task_id}"
        elif event.actor_name == "reviewer":
            # #953 — CLI approve is the canonical path for human review;
            # surface it first. Spinning up a reviewer worker session is
            # a valid alternative but rarely what users actually want.
            try_hint = (
                f"Try: pm task approve {event.task_id} --actor {event.actor_name} --reason \"...\" "
                f"(or pm worker-start --role {event.actor_name} {event.project} to spin up a reviewer session) "
                f"(or pm task claim {event.task_id} for a per-task worker)"
            )
        else:
            try_hint = (
                f"Try: pm worker-start --role {event.actor_name} {event.project} "
                f"(or pm task claim {event.task_id} for a per-task worker)"
            )
    else:
        actor_display = event.actor_name or event.actor_type.value
        try_hint = f"Try: pm task claim {event.task_id}"
    message = (
        f"Task {event.task_id} was routed to {actor_display} but no "
        f"matching session is running. {try_hint}"
    )
    try:
        store.upsert_alert(
            "task_assignment",
            _alert_type_for(event),
            "warning",
            message,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "task_assignment_notify: failed to raise alert for %s",
            event.task_id, exc_info=True,
        )


def _escalate_no_session_service(event: TaskAssignmentEvent, store: Any | None) -> None:
    """No session service at all — dev/test mode or misconfig. Surface once."""
    if store is None:
        return
    try:
        store.upsert_alert(
            "task_assignment",
            "no_session_service",
            "warning",
            (
                "Task-assignment notify cannot resolve a session service. "
                "Check plugin host configuration — pings will not be delivered "
                f"(example pending task: {event.task_id})."
            ),
        )
    except Exception:  # noqa: BLE001
        pass
