"""Work-service resolution helpers for cockpit inbox actions."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def open_work_service_for_task(config: Any, task_id: str) -> Any | None:
    """Open the work service for the registered project owning ``task_id``.

    Backend-aware (#1812). On the sqlite backend we still gate on the
    per-project ``.pollypm/state.db`` existing on disk — that's the file
    the service will read. On the pg backend there is no per-project
    file; the only gate is "the task's project key is registered" so
    :func:`create_work_service` is asked for a service against the
    workspace-wide pg store with ``project_key`` set. The previous
    blanket ``db_path.exists()`` short-circuit silently disabled the
    primary resolver path under pg.
    """
    project_key = task_id.split("/", 1)[0]
    project = getattr(config, "projects", {}).get(project_key)
    if project is None:
        return None
    # Typed helper routes through the doubled-pollypm-path guard (#1972).
    from pollypm.projects import project_state_db_path

    db_path = project_state_db_path(project.path)
    try:
        from pollypm.work.factory import _resolve_backend, create_work_service
    except Exception:  # noqa: BLE001
        return None

    backend = _resolve_backend(config)
    if backend != "postgres" and not db_path.exists():
        # sqlite-only gate kept: there is literally no DB file to open.
        # On pg the file is irrelevant so we skip this check.
        return None
    try:
        # Forward ``config`` so ``[storage] backend`` is honoured (#1369,
        # #1737). ``db_path`` is ignored on the pg backend per the factory
        # docstring; on sqlite the per-project file is used as before.
        return create_work_service(
            config=config,
            db_path=db_path if backend != "postgres" else None,
            project_path=project.path,
            project_key=project_key,
        )
    except Exception:  # noqa: BLE001
        return None


def resolve_inbox_work_service(config: Any, item: Any, task_id: str) -> Any | None:
    """Resolve a work service for a cockpit inbox row.

    The task-id project key is tried first. If that does not map to a
    registered project, or if the registered service does not contain
    the task that produced the inbox row, the inbox entry's source
    ``db_path`` is used as a best-effort fallback.

    Backend-agnostic (#1812). Previously this helper used a sqlite-only
    ``svc._db_path`` getattr to short-circuit the ``svc.get(task_id)``
    probe; under pg that attribute is missing, so ``same_db`` was always
    ``False`` and the probe always ran (which is the safer default
    anyway). Now we always probe — the cost is one extra select per
    inbox row and the resolver behaves identically across backends.
    """
    db_path = getattr(item, "db_path", None) if item is not None else None
    svc = open_work_service_for_task(config, task_id)
    if svc is not None:
        try:
            svc.get(task_id)
            return svc
        except Exception:  # noqa: BLE001
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                pass
            logger.debug(
                "cockpit inbox: registered project svc did not contain %s; "
                "falling back to source db_path=%r",
                task_id,
                db_path,
                exc_info=True,
            )
    if db_path is not None:
        try:
            from pollypm.work.factory import create_work_service

            return create_work_service(
                config=config,
                db_path=db_path,
                project_path=Path(db_path).parent.parent,
            )
        except Exception:  # noqa: BLE001
            pass
    logger.warning(
        "cockpit inbox: svc unresolved for task_id=%s project_key=%r scope=%r db_path=%r",
        task_id,
        getattr(item, "project", None) if item is not None else None,
        getattr(item, "scope", None) if item is not None else None,
        getattr(item, "db_path", None) if item is not None else None,
    )
    return None


__all__ = ["open_work_service_for_task", "resolve_inbox_work_service"]
