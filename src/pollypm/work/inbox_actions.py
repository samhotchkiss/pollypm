"""Work-service resolution helpers for cockpit inbox actions."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def open_work_service_for_task(config: Any, task_id: str) -> Any | None:
    """Open the work service for the registered project owning ``task_id``."""
    project_key = task_id.split("/", 1)[0]
    project = getattr(config, "projects", {}).get(project_key)
    if project is None:
        return None
    db_path = project.path / ".pollypm" / "state.db"
    if not db_path.exists():
        return None
    try:
        from pollypm.work.sqlite_service import SQLiteWorkService

        return SQLiteWorkService(db_path=db_path, project_path=project.path)
    except Exception:  # noqa: BLE001
        return None


def resolve_inbox_work_service(config: Any, item: Any, task_id: str) -> Any | None:
    """Resolve a work service for a cockpit inbox row.

    The task-id project key is tried first. If that does not map to a
    registered project DB, the inbox entry's source ``db_path`` is used
    as a best-effort fallback.
    """
    svc = open_work_service_for_task(config, task_id)
    if svc is not None:
        return svc
    db_path = getattr(item, "db_path", None) if item is not None else None
    if db_path is not None:
        try:
            from pollypm.work.sqlite_service import SQLiteWorkService

            return SQLiteWorkService(
                db_path=db_path,
                project_path=Path(db_path).parent.parent,
            )
        except Exception:  # noqa: BLE001
            pass
    logger.warning(
        "cockpit inbox: svc unresolved for task_id=%s project_name=%r scope=%r db_path=%r",
        task_id,
        getattr(item, "project", None) if item is not None else None,
        getattr(item, "scope", None) if item is not None else None,
        getattr(item, "db_path", None) if item is not None else None,
    )
    return None


__all__ = ["open_work_service_for_task", "resolve_inbox_work_service"]
