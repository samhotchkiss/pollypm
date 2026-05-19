"""Small work-service construction helpers for non-CLI callers."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def open_project_work_service(project: Any, *, config: Any = None) -> Any | None:
    """Open a per-project work service, returning None on failure.

    Routes through :func:`pollypm.work.factory.create_work_service` so the
    operator's ``[storage] backend`` toml setting is honoured (#1369,
    #1737). ``db_path`` is sqlite-specific and ignored on the pg backend.
    ``config`` is optional; when omitted it is loaded on demand so the
    pg dispatch path still fires for callers that haven't been threaded
    yet (the canonical pattern is to pass the already-loaded config in).
    """
    project_path = getattr(project, "path", None)
    if project_path is None:
        return None
    db_path = Path(project_path) / ".pollypm" / "state.db"
    try:
        if not db_path.exists():
            return None
    except OSError:
        return None
    if config is None:
        try:
            from pollypm.config import load_config

            config = load_config()
        except Exception:  # noqa: BLE001
            config = None
    try:
        from pollypm.work.factory import create_work_service

        return create_work_service(
            config=config,
            db_path=db_path,
            project_path=Path(project_path),
            project_key=getattr(project, "key", None),
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "work.service_factory: open work service failed for %s",
            getattr(project, "key", "?"),
            exc_info=True,
        )
        return None


__all__ = ["open_project_work_service"]
