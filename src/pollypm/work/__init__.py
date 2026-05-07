# Work service: sealed work management for PollyPM.
"""Public construction surface for the SQLite-backed work service.

Canonical usage outside ``pollypm.work.*``::

    from pollypm.work import create_work_service

    with create_work_service(project_path=project.path) as svc:
        ...

The factory resolves the canonical DB path via
:func:`pollypm.work.db_resolver.resolve_work_db_path` and constructs the
:class:`pollypm.work.sqlite_service.SQLiteWorkService`.

Direct construction of ``SQLiteWorkService`` from outside the work
package is discouraged: it forces every callsite to know the on-disk
layout (workspace-vs-per-project, dual-DB confusion, etc.) and bypasses
future resolver enhancements (env overrides, fallbacks, telemetry).

Escape valve: callers that genuinely need a non-canonical path (legacy
migration, tests, explicit ``--db`` overrides) may pass ``db_path=...``
explicitly. That makes the deviation visible at the callsite.
"""

from pollypm.work.factory import create_work_service

__all__ = ["create_work_service"]
