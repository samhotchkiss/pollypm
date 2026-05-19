"""Read-only state probes used by ``pm doctor`` (Postgres-only).

Following Slice K-state-callers-port (#1737), these probes only run
against the Postgres backend. The legacy sqlite branches have been
removed. Function signatures still accept ``db_path`` / ``table``
parameters for caller compatibility but they are no longer used.

Every probe is:

* **read-only** — issued against the process-wide RO pool;
* **best-effort** — pool / query failures degrade to the documented
  sentinel (``None``, ``False``, or ``0``) instead of propagating.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pollypm.models import PollyPMConfig

logger = logging.getLogger(__name__)


def _pg_ro_conn(config: "PollyPMConfig | None" = None):
    """Open a pool-borrowed RO connection or return ``None``.

    Every probe must degrade silently when the backend is unreachable
    so a doctor run never throws on a transient pool / network blip.
    """
    try:
        from pollypm.storage.pg_pool import get_ro_pool
    except Exception as exc:  # noqa: BLE001
        logger.debug("doctor_state_probes: pg_pool import failed: %s", exc)
        return None
    try:
        return get_ro_pool(config).connection()
    except Exception as exc:  # noqa: BLE001
        logger.debug("doctor_state_probes: get_ro_pool failed: %s", exc)
        return None


def applied_schema_version_ro(
    db_path: Path,
    table: str,
    *,
    config: "PollyPMConfig | None" = None,
) -> int | None:
    """Return ``MAX(version)`` from ``schema_migrations``, or ``None``.

    On the pg backend both ``schema_version`` and ``work_schema_version``
    map to the unified ``schema_migrations`` table — the pg port
    collapsed the two-version split into a single forward-only migration
    list. Callers passing either ``table`` name get back the same
    ``MAX(version)`` so existing doctor checks keep working. The
    ``db_path`` and ``table`` arguments are unused on the pg backend
    but kept for caller compatibility.
    """
    del db_path, table  # unused on pg backend
    ctx = _pg_ro_conn(config)
    if ctx is None:
        return None
    try:
        with ctx as conn, conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                )
                row = cur.fetchone()
            except Exception:  # noqa: BLE001
                return None
            return int(row[0]) if row and row[0] is not None else 0
    except Exception:  # noqa: BLE001
        return None


def count_work_tasks_ro(
    db_path: Path,
    *,
    config: "PollyPMConfig | None" = None,
) -> int | None:
    """Return ``COUNT(*) FROM work_tasks``, or ``None``.

    Returns ``None`` when the ``work_tasks`` table does not exist on the
    pg schema. Returns ``0`` when the table exists but is empty. Used by
    the dual-DB drift probe to tell "no work tables" apart from "tables
    present but empty". ``db_path`` is unused on the pg backend.
    """
    del db_path  # unused on pg backend
    ctx = _pg_ro_conn(config)
    if ctx is None:
        return None
    try:
        with ctx as conn, conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'work_tasks'"
                )
                if cur.fetchone() is None:
                    return None
                cur.execute("SELECT COUNT(*) FROM work_tasks")
                row = cur.fetchone()
            except Exception:  # noqa: BLE001
                return None
            return int(row[0]) if row and row[0] is not None else 0
    except Exception:  # noqa: BLE001
        return None


def has_messages_table_ro(
    db_path: Path,
    *,
    config: "PollyPMConfig | None" = None,
) -> bool:
    """Return ``True`` when the pg schema carries a ``messages`` table.

    Read-only probe used by the dual-DB drift check. Returns ``False``
    on any pool / query failure. ``db_path`` is unused on the pg backend.
    """
    del db_path  # unused on pg backend
    ctx = _pg_ro_conn(config)
    if ctx is None:
        return False
    try:
        with ctx as conn, conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'messages'"
                )
                return cur.fetchone() is not None
            except Exception:  # noqa: BLE001
                return False
    except Exception:  # noqa: BLE001
        return False


def sessions_row_count_ro(
    db_path: Path,
    *,
    config: "PollyPMConfig | None" = None,
) -> int | None:
    """Return ``COUNT(*) FROM sessions``, or ``None``.

    Returns ``None`` when the ``sessions`` table does not exist
    (a fresh install before ``Supervisor.start()`` runs
    ``repair_sessions_table()``). Returns ``0`` when the table exists
    but is empty — the ``check_sessions_table_populated`` doctor check
    treats that as a distinct fail signal so the operator gets a clear
    "repair didn't run" hint instead of a silent skip.
    """
    del db_path  # unused on pg backend
    ctx = _pg_ro_conn(config)
    if ctx is None:
        return None
    try:
        with ctx as conn, conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'sessions'"
                )
                if cur.fetchone() is None:
                    return None
                cur.execute("SELECT COUNT(*) FROM sessions")
                row = cur.fetchone()
            except Exception:  # noqa: BLE001
                return None
            return int(row[0]) if row and row[0] is not None else 0
    except Exception:  # noqa: BLE001
        return None


def session_window_names_ro(
    db_path: Path,
    *,
    config: "PollyPMConfig | None" = None,
) -> set[str] | None:
    """Return the set of ``window_name`` values in ``sessions``, or ``None``.

    Returns ``None`` when the ``sessions`` table is missing — distinct
    from the empty-set case (table present, no rows) so the
    session-drift doctor check can skip cleanly when the table itself
    has not been provisioned yet.
    """
    del db_path  # unused on pg backend
    ctx = _pg_ro_conn(config)
    if ctx is None:
        return None
    try:
        with ctx as conn, conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'sessions'"
                )
                if cur.fetchone() is None:
                    return None
                cur.execute("SELECT window_name FROM sessions")
                windows: set[str] = set()
                for row in cur.fetchall():
                    if row and row[0]:
                        windows.add(str(row[0]))
                return windows
            except Exception:  # noqa: BLE001
                return None
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "applied_schema_version_ro",
    "count_work_tasks_ro",
    "has_messages_table_ro",
    "sessions_row_count_ro",
    "session_window_names_ro",
]
