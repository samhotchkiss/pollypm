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
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pollypm.models import PollyPMConfig

logger = logging.getLogger(__name__)


# #1634 #1944 perf — per-process TTL cache for ``activity_feed_transition_rows``.
#
# On the pg backend the function ignores ``db_path`` and runs a single
# global ``SELECT ... FROM work_transitions ...`` query. The activity-feed
# rail badge builds an :class:`EventProjector` which then loops over
# ``self._work_dbs`` and calls this helper once per configured project —
# producing N identical global queries for one badge update on a
# workspace with N projects. The downstream filter/sort/dedupe layer
# in :mod:`event_projector` then has to chew through N copies of the
# same rows before slicing to the limit.
#
# Mirrors PR #1907's ``pm_inbox_awaits_user_list`` wedge and PR #1928's
# ``Supervisor.status()`` wedge: a tiny module-level dict keyed on
# ``(id(config), since_ts, limit)`` with a short TTL. ``db_path`` is
# intentionally excluded from the key because the pg implementation
# already ignores it — every per-project call within a rail tick
# resolves to the same global result. The cockpit router invalidates
# its config cache on mtime change, so a reload yields a fresh
# ``id(config)`` and bypasses this cache automatically. Callers
# receive a fresh ``list`` of row dicts so downstream mutation cannot
# leak into the cached snapshot.
_ACTIVITY_FEED_ROWS_TTL_SECONDS = 1.0
_ACTIVITY_FEED_ROWS_CACHE: dict[
    tuple[int, str | None, int], tuple[float, tuple[dict[str, Any], ...]]
] = {}


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
    """Return recent work-transition rows for activity-feed projection.

    #1634 #1944 perf — results are memoised per ``(id(config), since_ts,
    limit)`` with a short TTL (:data:`_ACTIVITY_FEED_ROWS_TTL_SECONDS`)
    so the activity-feed projector's per-project ``_work_dbs`` loop
    collapses onto a single global query instead of repeating identical
    reads (``db_path`` is unused on the pg backend). Callers receive a
    fresh list of row dicts so downstream mutation is safe.
    """
    del db_path  # unused on pg backend

    cache_key = (id(config), since_ts, int(limit))
    now = time.monotonic()
    cached = _ACTIVITY_FEED_ROWS_CACHE.get(cache_key)
    if cached is not None and now - cached[0] < _ACTIVITY_FEED_ROWS_TTL_SECONDS:
        return [dict(row) for row in cached[1]]

    result = _activity_feed_transition_rows_uncached(
        since_ts=since_ts, limit=limit, config=config,
    )
    # Best-effort eviction so the cache doesn't grow across long-lived
    # processes that build successive configs or vary ``since_ts``.
    if len(_ACTIVITY_FEED_ROWS_CACHE) > 16:
        for stale_key in [
            k for k, (ts, _v) in _ACTIVITY_FEED_ROWS_CACHE.items()
            if now - ts >= _ACTIVITY_FEED_ROWS_TTL_SECONDS
        ]:
            _ACTIVITY_FEED_ROWS_CACHE.pop(stale_key, None)
    _ACTIVITY_FEED_ROWS_CACHE[cache_key] = (
        now, tuple(dict(row) for row in result),
    )
    return result


def _activity_feed_transition_rows_uncached(
    *,
    since_ts: str | None,
    limit: int,
    config: "PollyPMConfig | None" = None,
) -> list[dict[str, Any]]:
    """Underlying implementation of :func:`activity_feed_transition_rows`.

    Separated from the public entry point so the cache wrapper stays
    trivially readable. Tests that need to bypass the cache can call
    this directly (or clear :data:`_ACTIVITY_FEED_ROWS_CACHE`).
    """
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
