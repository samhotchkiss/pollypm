"""Work-task state probes for UI and plugin callers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pollypm.storage.work_task_state import (
    blocked_since_stamp,
    blocker_chain_statuses,
    has_work_task_rows,
    project_activity_probe,
    task_numbers_with_statuses,
    task_status_probe,
)

ACTIVE_TASK_STATUSES: frozenset[str] = frozenset({"in_progress", "rework"})
USER_WAITING_STATUSES: frozenset[str] = frozenset({
    "blocked",
    "on_hold",
    "waiting_on_user",
})
TERMINAL_TASK_STATUSES: frozenset[str] = frozenset({"done", "cancelled"})


def _workspace_root(config: Any) -> Path | None:
    value = getattr(getattr(config, "project", None), "workspace_root", None)
    return Path(value) if value is not None else None


def _project_key(project: Any) -> str | None:
    key = getattr(project, "key", None)
    return str(key) if key else None


def active_task_numbers(project: Any, *, config: Any = None) -> list[int]:
    """Return DB-backed task numbers considered actively worked."""
    project_key = _project_key(project)
    project_path = getattr(project, "path", None)
    if not project_key or project_path is None:
        return []
    return task_numbers_with_statuses(
        project_key=project_key,
        project_path=Path(project_path),
        statuses=ACTIVE_TASK_STATUSES,
        workspace_root=_workspace_root(config),
    )


def user_waiting_task_ids(config: Any) -> frozenset[str]:
    """Return ``project/N`` ids for tracked tasks waiting on the user."""
    out: set[str] = set()
    for project_key, project in getattr(config, "projects", {}).items():
        if not getattr(project, "tracked", False):
            continue
        project_path = getattr(project, "path", None)
        if project_path is None:
            continue
        numbers = task_numbers_with_statuses(
            project_key=str(project_key),
            project_path=Path(project_path),
            statuses=USER_WAITING_STATUSES,
            workspace_root=None,
        )
        out.update(f"{project_key}/{number}" for number in numbers)
    return frozenset(out)


def has_work_tasks_in_db(
    db_path: Path,
    *,
    project_key: str | None = None,
) -> bool:
    """Return whether a work DB has task rows without exposing SQLite to callers."""
    return has_work_task_rows(Path(db_path), project_key=project_key)


def project_activity(
    *,
    project_key: str,
    project: Any,
    cutoff_iso: str,
    config: Any = None,
) -> tuple[bool, bool]:
    """Return ``(is_active, has_working_task)`` from work-task rows."""
    project_path = getattr(project, "path", None)
    if project_path is None:
        return False, False
    return project_activity_probe(
        project_key=str(project_key),
        project_path=Path(project_path),
        cutoff_iso=cutoff_iso,
        workspace_root=_workspace_root(config),
    )


def parse_task_window_name(name: str) -> tuple[str, int] | None:
    """Parse ``task-<project>-<N>`` into ``(project, N)``."""
    if not name.startswith("task-"):
        return None
    suffix = name[len("task-"):]
    project, sep, number = suffix.rpartition("-")
    if not sep or not project or not number.isdigit():
        return None
    return project, int(number)


def task_window_terminal_or_missing(config: Any, name: str) -> bool:
    """Return True if ``name``'s task row is terminal or orphaned."""
    parsed = parse_task_window_name(name)
    if parsed is None:
        return False
    project_key, task_number = parsed

    projects = getattr(config, "projects", None) if config is not None else None
    if not projects:
        return False
    project = projects.get(project_key)
    if project is None:
        return True
    project_path = getattr(project, "path", None)
    if project_path is None:
        return False

    _found_any_db, status = task_status_probe(
        project_key=project_key,
        task_number=task_number,
        project_path=Path(project_path),
        workspace_root=_workspace_root(config),
    )
    if status is None:
        return True
    return status in TERMINAL_TASK_STATUSES


def blocked_since_stamp_for_service(
    work: Any,
    *,
    project_key: str,
    task_number: int,
    blocked_status: str,
) -> object | None:
    """Return the raw blocked timestamp from a work-service connection."""
    conn = getattr(work, "_conn", None)
    if conn is None:
        return None
    return blocked_since_stamp(
        conn,
        project_key=project_key,
        task_number=task_number,
        blocked_status=blocked_status,
    )


def blocker_chain_for_service(
    work: Any,
    *,
    project_key: str,
    task_number: int,
) -> tuple[set[tuple[str, int]], dict[tuple[str, int], str]]:
    """Return recursive blocker keys and statuses from a work service."""
    conn = getattr(work, "_conn", None)
    if conn is None:
        return set(), {}
    return blocker_chain_statuses(
        conn,
        project_key=project_key,
        task_number=task_number,
    )


__all__ = [
    "ACTIVE_TASK_STATUSES",
    "TERMINAL_TASK_STATUSES",
    "USER_WAITING_STATUSES",
    "active_task_numbers",
    "blocked_since_stamp_for_service",
    "blocker_chain_for_service",
    "has_work_tasks_in_db",
    "parse_task_window_name",
    "project_activity",
    "task_window_terminal_or_missing",
    "user_waiting_task_ids",
]
