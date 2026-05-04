"""Small work-service construction helpers for non-CLI callers."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def open_project_work_service(project: Any) -> Any | None:
    """Open a per-project SQLite work service, returning None on failure."""
    project_path = getattr(project, "path", None)
    if project_path is None:
        return None
    db_path = Path(project_path) / ".pollypm" / "state.db"
    try:
        if not db_path.exists():
            return None
    except OSError:
        return None
    try:
        from pollypm.work.sqlite_service import SQLiteWorkService

        return SQLiteWorkService(db_path=db_path, project_path=Path(project_path))
    except Exception:  # noqa: BLE001
        logger.debug(
            "work.service_factory: open work service failed for %s",
            getattr(project, "key", "?"),
            exc_info=True,
        )
        return None


__all__ = ["open_project_work_service"]
