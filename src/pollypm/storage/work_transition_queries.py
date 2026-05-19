"""Read-only work-transition query helpers (Postgres-only).

Plugin and presentation layers should not open workspace databases
directly. This module owns the raw connection and schema details for
small projection-style reads that are not yet on a richer work-service
API. Following Slice K-state-callers-port (#1737), only the Postgres
path remains. ``db_path`` is kept in signatures for caller compatibility
but is unused.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pollypm.models import PollyPMConfig

logger = logging.getLogger(__name__)


_ADVISOR_TRANSITION_PG_SQL = (
    "SELECT t.task_project AS project, t.task_number AS task_number, "
    "       COALESCE(w.title, '') AS title, "
    "       t.from_state AS from_state, t.to_state AS to_state, "
    "       t.actor AS actor, t.created_at AS created_at "
    "FROM work_transitions t "
    "LEFT JOIN work_tasks w "
    "  ON w.project = t.task_project AND w.task_number = t.task_number "
    "WHERE t.task_project = %s AND t.created_at::text >= %s "
    "ORDER BY t.created_at ASC"
)


def advisor_transition_rows(
    db_path: Path,
    *,
    project_key: str,
    since_iso: str,
    config: "PollyPMConfig | None" = None,
) -> list[dict[str, Any]]:
    """Return transition rows for advisor change detection."""
    del db_path  # unused on pg backend
    try:
        from pollypm.storage.pg_pool import get_ro_pool
    except Exception as exc:  # noqa: BLE001
        logger.debug("work_transition_queries: pg_pool import failed: %s", exc)
        return []
    try:
        pool = get_ro_pool(config)
    except Exception as exc:  # noqa: BLE001
        logger.debug("work_transition_queries: get_ro_pool failed: %s", exc)
        return []
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(_ADVISOR_TRANSITION_PG_SQL, (project_key, since_iso))
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "work_transition_queries: pg advisor query failed for %s: %s",
            project_key, exc,
        )
        return []
    return [dict(zip(cols, row, strict=False)) for row in rows]


def activity_feed_transition_rows(
    db_path: Path,
    *,
    since_ts: str | None,
    limit: int,
    config: "PollyPMConfig | None" = None,
) -> list[dict[str, Any]]:
    """Return recent work-transition rows for activity-feed projection."""
    del db_path  # unused on pg backend
    try:
        from pollypm.storage.pg_pool import get_ro_pool
    except Exception as exc:  # noqa: BLE001
        logger.debug("work_transition_queries: pg_pool import failed: %s", exc)
        return []
    try:
        pool = get_ro_pool(config)
    except Exception as exc:  # noqa: BLE001
        logger.debug("work_transition_queries: get_ro_pool failed: %s", exc)
        return []
    params: list[Any] = []
    where = ""
    if since_ts is not None:
        where = "WHERE created_at::text >= %s"
        params.append(since_ts)
    sql = (
        "SELECT id, task_project, task_number, from_state, to_state, "
        f"actor, reason, created_at FROM work_transitions {where} "
        "ORDER BY id DESC LIMIT %s"
    )
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (*params, int(limit)))
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "work_transition_queries: pg activity-feed query failed: %s", exc,
        )
        return []
    return [dict(zip(cols, row, strict=False)) for row in rows]


__all__ = [
    "activity_feed_transition_rows",
    "advisor_transition_rows",
]
