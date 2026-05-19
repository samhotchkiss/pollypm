"""Read-only ``work_sessions`` aggregate queries (Postgres-only).

Presentation/plugin code should not open workspace databases directly;
this module owns the schema/connection details for small projection
reads against ``work_sessions``.

Following Slice K-state-callers-port (#1737), this module talks to the
Postgres RO pool only. The ``db_path`` parameter is kept for caller
compatibility but is unused.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

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

    Returns ``None`` if the pool / query can't be reached so the Tokens
    line in the per-project dashboard can degrade to ``(n/a)`` instead
    of breaking the render. The ``COALESCE(SUM(...), 0)`` form yields
    ``(0, 0)`` consistently for the empty-table case. ``db_path`` is
    unused on the pg backend.
    """
    del db_path  # unused on pg backend
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
