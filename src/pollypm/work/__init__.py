# Work service: sealed work management for PollyPM.
"""Public construction surface for the configured work-service backend.

Canonical usage outside ``pollypm.work.*``::

    from pollypm.work import create_work_service

    with create_work_service(project_path=project.path, config=config) as svc:
        ...

The factory reads ``config.storage.backend`` and dispatches to the
matching backend implementation. After the #1737 cutover (issue #1939)
the default is ``"postgres"`` — :class:`pollypm.work.pg_service.PgWorkService`,
wired against :mod:`pollypm.storage.pg_pool`. ``"sqlite"`` remains
registered for explicit opt-in (tests, legacy migration); it constructs
:class:`pollypm.work.sqlite_service.SQLiteWorkService` against the path
resolved by :func:`pollypm.work.db_resolver.resolve_work_db_path`.

Direct construction of either concrete service from outside the work
package is discouraged: it forces every callsite to know the on-disk
layout (workspace-vs-per-project, dual-DB confusion, etc.) and bypasses
future resolver enhancements (env overrides, fallbacks, telemetry).

Escape valve: callers that genuinely need a non-canonical sqlite path
(legacy migration, tests, explicit ``--db`` overrides) may pass
``db_path=...`` explicitly. That makes the deviation visible at the
callsite AND forces sqlite dispatch — without an explicit ``db_path``
the factory honours ``config.storage.backend``.
"""

from pollypm.work.factory import create_work_service

__all__ = ["create_work_service"]
