"""Read-only ``work_sessions`` aggregate queries.

Presentation/plugin code should not open workspace databases directly;
this module owns the schema/connection details for small projection
reads against ``work_sessions``.

The aggregate used by the per-project dashboard's Tokens line lives
here so the rendering layer (``cockpit_sections``) no longer has to
``import sqlite3`` or know the table name.

Backend dispatch (#1737)
------------------------

When ``[storage] backend = "postgres"`` is active, the same aggregate
runs against the process-wide read-only pool keyed on the
``task_project`` column (the pg schema uses the same column name as the
sqlite shape — see :mod:`pollypm.storage.pg_schema`). The sqlite branch
is preserved verbatim so first-run installs keep their existing
``file:<path>?mode=ro`` semantics, including the ``apply_workspace_pragmas``
busy-timeout setup that #1018 introduced.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from pollypm.storage._backend_dispatch import is_pg_backend

if TYPE_CHECKING:
    from pollypm.models import PollyPMConfig

logger = logging.getLogger(__name__)


def aggregate_project_session_tokens(
    db_path: Path,
    *,
    project_key: str,
    config: "PollyPMConfig | None" = None,
) -> tuple[int, int] | None:
    """Return ``(SUM(total_input_tokens), SUM(total_output_tokens))`` for ``project_key``.

    Returns ``None`` if the DB is missing or the query fails (e.g. the
    ``work_sessions`` table does not exist on an old workspace) so the
    Tokens line in the per-project dashboard can degrade to ``(n/a)``
    instead of breaking the render.

    A short ``busy_timeout`` is applied via the standard workspace
    pragmas because the cockpit reader runs alongside JobWorkerPool +
    heartbeat writers on the same DB (#1018).
    """
    if is_pg_backend(config):
        return _aggregate_pg(project_key=project_key, config=config)

    try:
        from pollypm.storage.legacy_per_project_db import (
            aggregate_project_session_tokens_ro,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "work_session_queries: legacy sqlite helper import failed: %s", exc,
        )
        return None
    return aggregate_project_session_tokens_ro(
        db_path, project_key=project_key,
    )


def _aggregate_pg(
    *,
    project_key: str,
    config: "PollyPMConfig | None",
) -> tuple[int, int] | None:
    """Postgres branch for :func:`aggregate_project_session_tokens`.

    Same shape as the sqlite branch: returns ``(in, out)`` tuple
    (zero-filled on empty rows) or ``None`` when the pool / query
    can't be reached.
    """
    try:
        from pollypm.storage.pg_pool import get_ro_pool
    except Exception as exc:  # noqa: BLE001
        logger.debug("work_session_queries: pg_pool import failed: %s", exc)
        return None
    try:
        pool = get_ro_pool(config)
    except Exception as exc:  # noqa: BLE001
        logger.debug("work_session_queries: get_ro_pool failed: %s", exc)
        return None
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(total_input_tokens), 0), "
                "       COALESCE(SUM(total_output_tokens), 0) "
                "FROM work_sessions WHERE task_project = %s",
                (project_key,),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.debug("work_session_queries: pg query failed: %s", exc)
        return None
    if row is None:
        return 0, 0
    return int(row[0] or 0), int(row[1] or 0)


__all__ = ["aggregate_project_session_tokens"]
