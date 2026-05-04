"""Shared plumbing for the core_recurring plugin family."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


def _resolve_config_path(payload: dict[str, Any]) -> Path:
    from pollypm.config import DEFAULT_CONFIG_PATH, resolve_config_path

    override = payload.get("config_path") if isinstance(payload, dict) else None
    return Path(override) if override else resolve_config_path(DEFAULT_CONFIG_PATH)


def _load_config(payload: dict[str, Any]):
    """Resolve + load the PollyPM config for a handler invocation."""
    from pollypm.config import load_config

    config_path = _resolve_config_path(payload)
    if not config_path.exists():
        raise RuntimeError(
            f"PollyPM config not found at {config_path}; cannot run recurring handler"
        )
    return load_config(config_path)


@contextmanager
def _load_config_and_store(payload: dict[str, Any]):
    """Yield ``(config, store)`` and close the store deterministically."""
    from pollypm.storage.state import StateStore

    config = _load_config(payload)
    store = StateStore(config.project.state_db)
    try:
        yield config, store
    finally:
        store.close()


def _open_msg_store(config: Any) -> Any:
    """Open the unified-messages Store for a handler invocation (#349)."""
    try:
        from pollypm.store.registry import get_store

        return get_store(config)
    except Exception:  # noqa: BLE001
        logger.debug(
            "core_recurring: unified Store unavailable", exc_info=True,
        )
        return None


def _close_msg_store(store: Any) -> None:
    """Close a Store handle opened by :func:`_open_msg_store`."""
    if store is None:
        return
    close = getattr(store, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001
            logger.debug("core_recurring: msg_store close raised", exc_info=True)


def _open_alert_exists(
    *,
    msg_store: Any,
    state_store: Any,
    session_name: str,
    alert_type: str,
) -> bool:
    """Return True when an open alert exists for ``(session, alert_type)``."""
    if msg_store is not None:
        try:
            rows = msg_store.query_messages(
                type="alert",
                scope=session_name,
                sender=alert_type,
                state="open",
                limit=1,
            )
        except Exception:  # noqa: BLE001
            return False
        return bool(rows)

    if state_store is None:
        return False
    open_alerts = getattr(state_store, "open_alerts", None)
    if not callable(open_alerts):
        return False
    try:
        rows = open_alerts()
    except Exception:  # noqa: BLE001
        return False
    for row in rows:
        if (
            getattr(row, "session_name", None) == session_name
            and getattr(row, "alert_type", None) == alert_type
            and getattr(row, "status", None) == "open"
        ):
            return True
    return False


# Ephemeral session name prefixes (#252). Sessions whose name starts with
# any of these are NOT in the supervisor's launch plan.
_EPHEMERAL_SESSION_PREFIXES: tuple[str, ...] = (
    "task-",
    "critic_",
    "downtime_",
)


def is_ephemeral_session_name(name: str) -> bool:
    """Return True if ``name`` matches an ephemeral session naming convention."""
    if not name:
        return False
    return any(name.startswith(prefix) for prefix in _EPHEMERAL_SESSION_PREFIXES)


def sweep_ephemeral_sessions(supervisor: Any, store: Any) -> dict[str, int]:
    """Mechanical health pass over ephemeral (non-planned) sessions (#252)."""
    summary = {
        "considered": 0,
        "alerts_raised": 0,
        "skipped_planned": 0,
        "zombie_task_windows_killed": 0,
    }

    try:
        planned_names = {
            launch.session.name for launch in supervisor.plan_launches()
        }
    except Exception:  # noqa: BLE001
        planned_names = set()

    session_service = getattr(supervisor, "session_service", None)
    if session_service is None:
        return summary

    try:
        handles = session_service.list()
    except Exception:  # noqa: BLE001
        logger.debug("ephemeral_sweep: session_service.list() failed", exc_info=True)
        return summary

    for handle in handles:
        name = getattr(handle, "name", "") or ""
        if not is_ephemeral_session_name(name):
            continue
        if name in planned_names:
            summary["skipped_planned"] += 1
            continue

        # #1002 — kill ``task-<project>-<N>`` windows whose DB task is
        # in a terminal state (done / cancelled) or whose row no longer
        # exists. The teardown_worker path (work-service node_done /
        # approve) already handles the in-band cleanup case for tasks
        # that have a worker_session row, but planning-spawned critics
        # and any other window-without-row leak through. This sweep
        # closes the gap so a missed kill doesn't leave a clickable
        # zombie row in the rail (and a confusing pane in the closet).
        if name.startswith("task-") and _task_is_terminal_or_missing(supervisor, name):
            if _kill_zombie_task_window(supervisor, handle):
                summary["zombie_task_windows_killed"] += 1
            # Fall through to the alert path so the dead pane (if any)
            # is still recorded; the kill itself is best-effort.

        summary["considered"] += 1
        try:
            health = session_service.health(name)
        except Exception:  # noqa: BLE001
            logger.debug(
                "ephemeral_sweep: health(%s) failed", name, exc_info=True,
            )
            continue

        failure: tuple[str, str] | None = None
        if not getattr(health, "window_present", False):
            failure = (
                "missing_window",
                f"Ephemeral session {name} has no tmux window",
            )
        elif getattr(health, "pane_dead", False):
            failure = (
                "pane_dead",
                f"Ephemeral session {name} pane has exited",
            )

        if failure is None:
            continue

        failure_kind, failure_message = failure
        alert_type = _ephemeral_alert_type(name, failure_kind)
        try:
            store.upsert_alert(
                name,
                alert_type,
                "warn",
                f"{failure_message}. "
                f"Why it matters: parent task is blocked because the "
                f"ephemeral session that was driving it is gone. "
                f"Fix: inspect the parent task's status and re-spawn the "
                f"session via the originating handler "
                f"(critic / downtime / `pm worker-start`).",
            )
            summary["alerts_raised"] += 1
        except Exception:  # noqa: BLE001
            logger.debug(
                "ephemeral_sweep: upsert_alert failed for %s", name,
                exc_info=True,
            )

    return summary


def _parse_task_window_name(name: str) -> tuple[str, int] | None:
    """Parse ``task-<project>-<N>`` into ``(project, N)``. Returns None on miss.

    Mirrors the construction in
    :func:`pollypm.work.session_manager.task_window_name` — keep in sync.
    """
    from pollypm.work.task_state import parse_task_window_name

    return parse_task_window_name(name)


def _task_is_terminal_or_missing(supervisor: Any, name: str) -> bool:
    """Return True if ``name``'s DB task is done/cancelled or absent.

    Looks up the project's ``state.db`` (per-project layout) and the
    workspace-root ``state.db`` (shared-DB layout) and inspects
    ``work_tasks``. We mirror
    :func:`pollypm.work.db_resolver.resolve_work_db_path` so a project
    whose tasks live only in the workspace-root DB still gets cleaned
    up correctly. A transient lookup failure (sqlite error, etc.)
    short-circuits to False so we never kill a live task window
    because we couldn't read the DB.
    """
    config = getattr(supervisor, "config", None)
    from pollypm.work.task_state import task_window_terminal_or_missing

    return task_window_terminal_or_missing(config, name)


def _kill_zombie_task_window(supervisor: Any, handle: Any) -> bool:
    """Kill ``handle``'s tmux window. Returns True iff the kill ran.

    Uses ``handle.tmux_session`` and ``handle.window_name`` to address
    the window so we don't assume the storage closet (per-task workers
    may live elsewhere in non-default deployments). Best-effort: any
    failure is swallowed so a misbehaving tmux can't break the heartbeat
    sweep for unrelated sessions.
    """
    session_service = getattr(supervisor, "session_service", None)
    if session_service is None:
        return False
    tmux = getattr(session_service, "tmux", None)
    if tmux is None:
        return False
    kill_window = getattr(tmux, "kill_window", None)
    if not callable(kill_window):
        return False
    tmux_session = getattr(handle, "tmux_session", None)
    window_name = getattr(handle, "window_name", None) or getattr(handle, "name", "")
    if not tmux_session or not window_name:
        return False
    target = f"{tmux_session}:{window_name}"
    try:
        kill_window(target)
        logger.info(
            "ephemeral_sweep: killed zombie task window %s "
            "(DB task is terminal or missing)",
            target,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "ephemeral_sweep: kill_window(%s) failed: %s",
            target, exc, exc_info=True,
        )
        return False


def _ephemeral_alert_type(session_name: str, failure_kind: str) -> str:
    """Pick a stable, parent-task-keyed alert type for an ephemeral failure."""
    if session_name.startswith("task-"):
        suffix = session_name[len("task-"):]
        if "-" in suffix:
            project, _, number = suffix.rpartition("-")
            if project and number.isdigit():
                return f"ephemeral_session_dead:{project}/{number}"
        return f"ephemeral_session_dead:{session_name}"
    if session_name.startswith("critic_"):
        return f"critic_failed:{session_name}"
    if session_name.startswith("downtime_"):
        return f"downtime_failed:{session_name}"
    return f"ephemeral_session_dead:{session_name}"
