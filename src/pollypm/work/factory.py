"""Canonical factory for the active work-service backend.

This module exists so callers outside ``pollypm.work.*`` do not have to
know how the work-service DB is resolved. Every direct
``SQLiteWorkService(...)`` callsite that lives in presentation, plugin,
or heartbeat code is suspect — see issue #1369. Migrating those callers
through this factory means the resolver is the single point of truth
for "where does work data live".

Backend dispatch (#1737, Slice A)
---------------------------------

Reads ``config.storage.backend``:

* ``"sqlite"`` (default) → :class:`pollypm.work.sqlite_service.SQLiteWorkService`,
  resolved via :func:`pollypm.work.db_resolver.resolve_work_db_path`.
* ``"postgres"`` → :class:`pollypm.work.pg_service.PgWorkService`, wired
  against the lazy pool singleton in :mod:`pollypm.storage.pg_pool`.

Any other value falls through to sqlite so a fat-fingered backend
string doesn't brick the CLI; the doctor's ``storage-backend`` check
surfaces the typo through its own error path.

Escape valve
------------
A caller that genuinely needs a non-canonical path (legacy migration,
explicit override, test fixture) may pass ``db_path=...`` directly.
That keeps the callsite visibly different from the canonical pattern,
which is the point: "this one is not using the resolver" should never
be invisible. ``db_path`` is sqlite-specific and is ignored on the pg
backend.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pollypm.config import PollyPMConfig
    from pollypm.work.service_dependencies import SyncManager


logger = logging.getLogger(__name__)


def _resolve_backend(config: "PollyPMConfig | None") -> str:
    """Return the configured backend string, defaulting to ``"sqlite"``.

    Reads ``config.storage.backend`` defensively — a missing attribute
    or non-string value falls back to sqlite. The doctor's
    ``storage-backend`` check surfaces real typos.
    """
    if config is None:
        return "sqlite"
    storage = getattr(config, "storage", None)
    if storage is None:
        return "sqlite"
    backend = getattr(storage, "backend", "sqlite")
    if not isinstance(backend, str) or not backend.strip():
        return "sqlite"
    return backend.strip()


def create_work_service(
    *,
    db_path: str | Path | None = None,
    project_path: str | Path | None = None,
    config: "PollyPMConfig | None" = None,
    project_key: str | None = None,
    sync_manager: "SyncManager | None" = None,
    session_manager: object | None = None,
) -> Any:
    """Construct the configured work-service backend.

    Parameters
    ----------
    db_path:
        Explicit DB path override for the sqlite backend. Ignored on
        ``backend="postgres"``. When ``None`` (the canonical case),
        the path is resolved via
        :func:`pollypm.work.db_resolver.resolve_work_db_path`.
    project_path:
        Filesystem path of the project, when known. Forwarded to the
        sqlite service constructor for project-aware operations (gates,
        activity logs, audit metadata). Ignored on the pg backend until
        Slice B re-introduces project_path-aware behaviour.
    config:
        Optional pre-loaded :class:`PollyPMConfig`. Forwarded to the
        resolver / pool factory to avoid hidden ``load_config()`` calls.
    project_key:
        Optional project key. Forwarded to the resolver so it can warn
        about stale per-project DB files (#1004), and to the pg service
        for the per-row ``project_key`` column.
    sync_manager / session_manager:
        Forwarded to the sqlite constructor for callers that need a
        bespoke sync or session manager (the heartbeat does this).
        Ignored on the pg backend until Slice B.

    Returns
    -------
    object
        A :class:`pollypm.work.service.WorkService` Protocol-satisfying
        instance. The concrete type depends on the configured backend.
    """
    backend = _resolve_backend(config)
    if backend == "postgres":
        from pollypm.work.pg_service import PgWorkService

        return PgWorkService(config=config, project_key=project_key)

    # sqlite (the default, and the fallback for unknown backends).
    from pollypm.work.sqlite_service import SQLiteWorkService

    resolved_path: Path
    if db_path is None:
        from pollypm.work.db_resolver import resolve_work_db_path

        resolved_path = resolve_work_db_path(project=project_key, config=config)
    else:
        resolved_path = Path(db_path)

    project_path_obj: Path | None = None
    if project_path is not None:
        project_path_obj = (
            project_path if isinstance(project_path, Path) else Path(project_path)
        )

    # Forward only the args that callers explicitly opted into. The
    # underlying ``SQLiteWorkService.__init__`` accepts ``sync_manager``
    # and ``session_manager`` as keyword args, but several test doubles
    # in the tree implement a narrower constructor signature
    # (``db_path``, ``project_path`` only). Passing ``None`` for the
    # optional managers in the factory's default path would TypeError
    # against those doubles, so we only forward when the caller asked.
    extra: dict[str, object] = {}
    if sync_manager is not None:
        extra["sync_manager"] = sync_manager
    if session_manager is not None:
        extra["session_manager"] = session_manager
    return SQLiteWorkService(
        db_path=resolved_path,
        project_path=project_path_obj,
        **extra,
    )


__all__ = ["create_work_service"]
