"""Canonical factory for :class:`SQLiteWorkService`.

This module exists so callers outside ``pollypm.work.*`` do not have to
know how the work-service DB path is resolved. Every direct
``SQLiteWorkService(...)`` callsite that lives in presentation, plugin,
or heartbeat code is suspect — see issue #1369. Migrating those callers
through this factory means the resolver is the single point of truth
for "where does work data live".

The factory is intentionally tiny: it composes
:func:`pollypm.work.db_resolver.resolve_work_db_path` with the
``SQLiteWorkService`` constructor. The audit emit added in #1343 lives
in the constructor itself, so every path through this factory still
records ``work_db.opened``.

Escape valve
------------
A caller that genuinely needs a non-canonical path (legacy migration,
explicit override, test fixture) may pass ``db_path=...`` directly.
That keeps the callsite visibly different from the canonical pattern,
which is the point: "this one is not using the resolver" should never
be invisible.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pollypm.config import PollyPMConfig
    from pollypm.work.service_dependencies import SyncManager
    from pollypm.work.sqlite_service import SQLiteWorkService


def create_work_service(
    *,
    db_path: str | Path | None = None,
    project_path: str | Path | None = None,
    config: "PollyPMConfig | None" = None,
    project_key: str | None = None,
    sync_manager: "SyncManager | None" = None,
    session_manager: object | None = None,
) -> "SQLiteWorkService":
    """Construct a :class:`SQLiteWorkService` for the canonical work DB.

    Parameters
    ----------
    db_path:
        Explicit DB path override. When ``None`` (the canonical case),
        the path is resolved via
        :func:`pollypm.work.db_resolver.resolve_work_db_path`. Pass a
        value here only when you genuinely need a non-canonical path
        (legacy migration, tests, explicit ``--db`` overrides).
    project_path:
        Filesystem path of the project, when known. Forwarded to the
        service constructor for project-aware operations (gates,
        activity logs, audit metadata).
    config:
        Optional pre-loaded :class:`PollyPMConfig`. Forwarded to the
        resolver to avoid a hidden ``load_config()`` call.
    project_key:
        Optional project key. Forwarded to the resolver so it can warn
        about stale per-project DB files (#1004).
    sync_manager / session_manager:
        Forwarded to the constructor for callers that need to inject a
        bespoke sync or session manager (the heartbeat does this).

    Returns
    -------
    SQLiteWorkService
        The constructed service. Use as a context manager to ensure
        the underlying connection is closed.
    """
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
