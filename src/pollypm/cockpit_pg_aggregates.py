"""Cross-project cockpit aggregates against the pg backend (issue #1737, Slice H).

The cockpit's hot paths (rail render, dashboard render, inbox badge) used
to fan out per-project file opens against sqlite — one ``state.db`` open
per project, plus a second ``SQLAlchemyStore`` open for the messages
table in the inbox-badge path. On a 12-project workspace that bottomed
out at 60-100 sqlite opens per refresh, which the perf reviews on #1634
flagged as the dominant rail latency.

Under postgres there is **one** database — the per-project fanout is
structurally unnecessary. This module collapses every call site into a
single pg query keyed on ``WHERE project IN (...)`` (or no filter at
all), then groups the result in Python.

Every entry point in this module is best-effort: a pg outage falls
through to ``None`` / empty so the caller can degrade rather than
crash the rail.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from pollypm.work.inbox_view import inbox_tasks

if TYPE_CHECKING:
    from pollypm.models import PollyPMConfig
    from pollypm.work.models import Task

logger = logging.getLogger(__name__)


# #1634 #1945 perf — per-process TTL cache for ``all_tasks_grouped``.
#
# Each rail ``build_items()`` tick runs ``_project_state_rollups``
# (which calls ``all_tasks_grouped(config)``) AND, when the rail's
# per-router 2s categorization TTL has expired, ``_project_categorizations``
# (which opens its own shared work-service and runs the same broad
# ``svc.list_tasks()`` against pg through :mod:`operator_view`). On
# overlapping rail builds the two paths produce two bulk task reads
# milliseconds apart. Caching ``all_tasks_grouped`` itself collapses
# repeat calls within a tick onto a single pg roundtrip; pair this
# with the ``_prefetch_project_state`` cache in :mod:`operator_view`
# to handle the second access path.
#
# Mirrors the same wedge PRs #1907 + #1928 used for
# ``pm_inbox_awaits_user_list`` and ``Supervisor.status()``: a tiny
# module-level dict keyed on ``id(config)`` with a short TTL. The
# cockpit router invalidates its config cache on mtime change, so a
# reload yields a fresh identity and bypasses this cache automatically.
# Callers receive a fresh dict + fresh inner lists so downstream
# mutation cannot leak into the cached snapshot.
_ALL_TASKS_GROUPED_TTL_SECONDS = 1.0
_ALL_TASKS_GROUPED_CACHE: dict[
    int, tuple[float, "dict[str, tuple[Task, ...]] | None"]
] = {}


# --------------------------------------------------------------------------- #
# Shared pg service handle
# --------------------------------------------------------------------------- #


def _open_pg_service(config: "PollyPMConfig | None") -> Any | None:
    """Return a fresh :class:`PgWorkService` or ``None`` on failure.

    The pg service is cheap to construct — it's a thin wrapper around the
    process-wide pool singleton — so we don't bother memoising here. The
    pool itself is the cache.
    """
    try:
        from pollypm.work.pg_service import PgWorkService
    except Exception:  # noqa: BLE001 - psycopg / pool import may fail
        logger.warning("pg aggregates: PgWorkService import failed", exc_info=True)
        return None
    try:
        return PgWorkService(config=config)
    except Exception:  # noqa: BLE001 - pool open / migration may fail
        logger.warning("pg aggregates: PgWorkService open failed", exc_info=True)
        return None


# --------------------------------------------------------------------------- #
# Task fanout collapse
# --------------------------------------------------------------------------- #


def all_tasks_grouped(
    config: "PollyPMConfig | None",
) -> dict[str, list["Task"]] | None:
    """Return ``{project_alias: [Task, ...]}`` for every task in pg.

    Single ``SELECT * FROM work_tasks`` query (no WHERE) instead of one
    per project. Callers that filter to a specific ``project_key`` do
    so against the resulting dict via :func:`project_storage_aliases`.

    Returns ``None`` when the pg backend isn't available so the caller
    falls back to its sqlite walk.

    #1634 #1945 perf — results are memoised per ``id(config)`` with a
    short TTL (:data:`_ALL_TASKS_GROUPED_TTL_SECONDS`) so the rail
    rollup path and any other broad-task caller within the same tick
    share a single pg roundtrip. Callers receive a fresh dict + fresh
    inner lists so downstream mutation is safe.
    """
    cache_key = id(config)
    now = time.monotonic()
    cached = _ALL_TASKS_GROUPED_CACHE.get(cache_key)
    if cached is not None and now - cached[0] < _ALL_TASKS_GROUPED_TTL_SECONDS:
        snapshot = cached[1]
        if snapshot is None:
            return None
        return {key: list(rows) for key, rows in snapshot.items()}

    result = _all_tasks_grouped_uncached(config)
    # Best-effort eviction so the cache doesn't grow across long-lived
    # processes with config reloads (each reload yields a fresh
    # ``id(config)``).
    if len(_ALL_TASKS_GROUPED_CACHE) > 8:
        for stale_key in [
            k for k, (ts, _v) in _ALL_TASKS_GROUPED_CACHE.items()
            if now - ts >= _ALL_TASKS_GROUPED_TTL_SECONDS
        ]:
            _ALL_TASKS_GROUPED_CACHE.pop(stale_key, None)
    snapshot = (
        None
        if result is None
        else {key: tuple(rows) for key, rows in result.items()}
    )
    _ALL_TASKS_GROUPED_CACHE[cache_key] = (now, snapshot)
    return result


def _all_tasks_grouped_uncached(
    config: "PollyPMConfig | None",
) -> dict[str, list["Task"]] | None:
    """Underlying implementation of :func:`all_tasks_grouped`.

    Separated from the public entry point so the cache wrapper stays
    trivially readable. Tests that need to bypass the cache can call
    this directly (or clear :data:`_ALL_TASKS_GROUPED_CACHE`).
    """
    svc = _open_pg_service(config)
    if svc is None:
        return None
    try:
        tasks = svc.list_tasks()
    except Exception:  # noqa: BLE001
        logger.warning("pg aggregates: list_tasks failed", exc_info=True)
        return None
    finally:
        close = getattr(svc, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass
    grouped: dict[str, list[Task]] = {}
    for task in tasks:
        key = getattr(task, "project", "") or ""
        grouped.setdefault(key, []).append(task)
    return grouped


def inbox_tasks_grouped(
    config: "PollyPMConfig | None",
) -> dict[str, list["Task"]] | None:
    """Return ``{project_alias: [inbox_task, ...]}`` across the workspace.

    One pg query (``list_nonterminal_tasks(project=None)``) feeds the
    inbox-membership filter once; partition is in Python. Replaces N
    independent ``inbox_tasks(svc, project=key)`` calls — one per
    tracked project — that the sqlite path runs today.
    """
    svc = _open_pg_service(config)
    if svc is None:
        return None
    try:
        items = inbox_tasks(svc, project=None)
    except Exception:  # noqa: BLE001
        logger.warning("pg aggregates: inbox_tasks failed", exc_info=True)
        return None
    finally:
        close = getattr(svc, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass
    grouped: dict[str, list[Task]] = {}
    for task in items:
        key = getattr(task, "project", "") or ""
        grouped.setdefault(key, []).append(task)
    return grouped


def _aliases(config: object, project_key: str) -> list[str]:
    """Resolve every form the pg row's ``project`` column may carry.

    Mirrors the sqlite path's :func:`pollypm.work.project_aliases.project_storage_aliases`
    so a project keyed ``my_blog`` in the config still matches a task
    whose ``project`` column was inserted as ``my-blog`` or ``MyBlog``.
    """
    try:
        from pollypm.work.project_aliases import project_storage_aliases

        return project_storage_aliases(config, project_key)
    except Exception:  # noqa: BLE001
        return [project_key]


def inbox_tasks_for_project(
    grouped: dict[str, list["Task"]] | None,
    config: object,
    project_key: str,
) -> list["Task"]:
    """Pluck one project's inbox tasks out of an :func:`inbox_tasks_grouped` result."""
    if not grouped:
        return []
    out: list[Task] = []
    seen: set[str] = set()
    for alias in _aliases(config, project_key):
        for task in grouped.get(alias, []):
            tid = getattr(task, "task_id", None)
            if tid and tid in seen:
                continue
            if tid:
                seen.add(tid)
            out.append(task)
    return out


def all_tasks_for_project(
    grouped: dict[str, list["Task"]] | None,
    config: object,
    project_key: str,
) -> list["Task"]:
    """Pluck one project's tasks out of an :func:`all_tasks_grouped` result."""
    if not grouped:
        return []
    out: list[Task] = []
    seen: set[str] = set()
    for alias in _aliases(config, project_key):
        for task in grouped.get(alias, []):
            tid = getattr(task, "task_id", None)
            if tid and tid in seen:
                continue
            if tid:
                seen.add(tid)
            out.append(task)
    return out


# --------------------------------------------------------------------------- #
# Inbox messages — replaces the per-project SQLAlchemyStore second-open.
# --------------------------------------------------------------------------- #


def open_messages(
    config: "PollyPMConfig | None",
    *,
    known_projects: set[str],
    limit: int | None = None,
) -> list[dict[str, Any]] | None:
    """Return every open inbox-shaped messages row in one pg query.

    Replaces the sqlite path's N-projects × ``SQLAlchemyStore.query_messages``
    fanout in :func:`pollypm.cockpit_inbox.pm_inbox_awaits_user_list`
    (the "dual-open" the perf review flagged): we go from two opens per
    project per refresh (work-service + store) to ONE pg query for
    messages plus one pg query for tasks, regardless of project count.

    The ``known_projects`` set scopes the SQL to scopes the user owns
    so a workspace row referencing a non-tracked project doesn't leak
    into the badge count.

    Pass ``limit`` to push a SQL ``LIMIT`` into the query (#1913). The
    prepaint preview path only needs the first dozen newest rows; the
    rail/cockpit unbounded paths still call without a limit.

    Returns ``None`` on pool / query failure so the caller can fall back
    to its per-project walk.
    """
    try:
        from pollypm.storage.pg_pool import get_ro_pool, get_rw_pool
    except Exception:  # noqa: BLE001
        logger.warning("pg aggregates: pg_pool import failed", exc_info=True)
        return None

    try:
        pool = get_ro_pool(config)
    except Exception:  # noqa: BLE001
        try:
            pool = get_rw_pool(config)
        except Exception:  # noqa: BLE001
            logger.warning("pg aggregates: pg pool open failed", exc_info=True)
            return None

    # The scope filter mirrors the sqlite path: include rows whose scope
    # is "inbox" (workspace-root surface), or whose scope matches one of
    # the tracked project keys. Empty scope falls through to the Python
    # filter so a row with NULL scope still surfaces if its payload
    # refs a known project.
    sql = (
        "SELECT id, scope, type, tier, recipient, sender, state, "
        "       subject, body, payload_json, labels, kind, "
        "       created_at, updated_at, closed_at "
        "  FROM messages "
        " WHERE recipient = %s "
        "   AND state = %s "
        "   AND type IN ('notify', 'inbox_task', 'alert')"
    )
    params: list[object] = ["user", "open"]
    if known_projects:
        sql += " AND (scope = '' OR scope = 'inbox' OR scope = ANY(%s))"
        params.append(list(known_projects))
    sql += " ORDER BY created_at DESC, id DESC"
    if limit is not None and limit > 0:
        sql += " LIMIT %s"
        params.append(int(limit))

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [desc.name if hasattr(desc, "name") else desc[0]
                    for desc in cur.description]
            raw = cur.fetchall()
    except Exception:  # noqa: BLE001
        logger.warning("pg aggregates: messages query failed", exc_info=True)
        return None

    rows: list[dict[str, Any]] = []
    for row_tuple in raw:
        row = dict(zip(cols, row_tuple))
        # Match SQLAlchemyStore's response shape: payload_json (jsonb)
        # is decoded; psycopg returns dict already, but a string-shaped
        # legacy import would need json.loads — keep the same defensive
        # decode the sqlite path does.
        payload = row.get("payload_json")
        if isinstance(payload, str):
            import json

            try:
                payload = json.loads(payload)
            except (ValueError, TypeError):
                payload = {}
        row["payload"] = payload if isinstance(payload, dict) else {}
        labels = row.get("labels")
        if isinstance(labels, str):
            import json

            try:
                labels = json.loads(labels)
            except (ValueError, TypeError):
                labels = []
        row["labels"] = labels if isinstance(labels, list) else []
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# Public API surface
# --------------------------------------------------------------------------- #


__all__ = [
    "all_tasks_for_project",
    "all_tasks_grouped",
    "inbox_tasks_for_project",
    "inbox_tasks_grouped",
    "open_messages",
]
