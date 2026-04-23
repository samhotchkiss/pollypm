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
    known_projects: tuple[Any, ...] = field(default_factory=tuple)
    enforce_plan: bool = True
    plan_dir: str = "docs/plan"


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
    # disambiguation. Best-effort.
    work_service: Any | None
    try:
        from pollypm.work.sqlite_service import SQLiteWorkService

        project_root = config.project.root_dir
        db_path = project_root / ".pollypm" / "state.db"
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
        project_root=config.project.root_dir,
        known_projects=known_projects,
        enforce_plan=config.planner.enforce_plan,
        plan_dir=config.planner.plan_dir,
        msg_store=msg_store,
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
    handle = index.resolve(event.actor_type, event.actor_name, event.project)

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

    # Dedupe: don't re-ping the same session about the same task within
    # the throttle window at the same execution_version.
    if store is not None and throttle_seconds > 0:
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

    message = format_ping_for_role(event)

    try:
        session_svc.send(target_name, message)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "task_assignment_notify: send to %s failed: %s", target_name, exc,
        )
        if store is not None:
            try:
                store.record_notification(
                    session_name=target_name,
                    task_id=event.task_id,
                    project=event.project,
                    message=message,
                    delivery_status=f"failed: {exc}"[:200],
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

    return {
        "outcome": "sent",
        "task_id": event.task_id,
        "session": target_name,
        "execution_version": execution_version,
    }


# ---------------------------------------------------------------------------
# Escalation helpers
# ---------------------------------------------------------------------------


def _alert_type_for(event: TaskAssignmentEvent) -> str:
    """Return the alert_type used to dedupe no-session alerts per task."""
    return f"no_session_for_assignment:{event.task_id}"


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
