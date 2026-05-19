"""Read-only state-DB probes used by ``pm doctor``.

The ``pollypm.doctor`` module surfaces a handful of small SQLite reads
against ``state.db`` (schema_version, work_schema_version, work_tasks
counts, sessions table inspection, messages-table presence). Per #1376
those reads should not be issued from the doctor module directly — it
sits above the storage layer and is not allowed to ``import sqlite3``
or open files via ``sqlite3.connect`` (the plugin-boundary contract
audited by
``tests/test_plugin_boundary_conformance.py::test_work_task_query_callers_do_not_open_sqlite_directly``).

Every probe here is:

* **read-only** (``file:<path>?mode=ro`` URI) — never mutates the DB,
  never holds a write lock; safe to run against live cockpit DBs;
* **best-effort** — missing file, missing table, or any
  ``sqlite3.Error`` returns the documented sentinel (``None``, ``False``,
  or ``0``) instead of propagating;
* **short busy_timeout** — a 1s ``timeout`` on the connect plus the
  standard workspace pragmas (``busy_timeout=...``) so a doctor run
  never blocks behind a JobWorkerPool writer.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from pollypm.storage._backend_dispatch import is_pg_backend
from pollypm.storage.sqlite_pragmas import apply_workspace_pragmas, readonly_uri

if TYPE_CHECKING:
    from pollypm.models import PollyPMConfig

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------- #
# Postgres branch (#1737, Slice C)
# --------------------------------------------------------------------- #
#
# All probes here key off ``state.db`` in the sqlite world. The pg port
# has a single shared schema (no per-project DB), so the doctor probes
# become straight SELECTs against the RO pool.
#
# The ``schema_version`` / ``work_schema_version`` table doesn't exist
# on the pg side — the pg migration applier uses ``schema_migrations``
# (version 1 = ``0001_initial`` which contains the merged schema).
# ``applied_schema_version_ro`` returns the max version from that table
# instead so the doctor check can still answer "schema is current".


def _pg_ro_conn(config: "PollyPMConfig | None" = None):
    """Open a pool-borrowed RO connection or return ``None``.

    Mirrors :func:`_connect_readonly`'s contract: every probe must
    degrade silently when the backend is unreachable so a doctor run
    never throws on a transient pool / network blip.
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


def _connect_readonly(db_path: Path) -> sqlite3.Connection | None:
    """Open ``db_path`` read-only or return ``None``.

    Returns ``None`` when the file does not exist or the connect call
    raises any ``sqlite3.Error``. Applies the workspace pragmas
    (``busy_timeout``) so concurrent writers don't starve the probe.
    """
    if not db_path.is_file():
        return None
    # #1674: percent-encode so URI metacharacters in the workspace path
    # don't break the read-only open.
    uri = readonly_uri(db_path)
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
    except sqlite3.Error as exc:
        logger.debug(
            "doctor_state_probes: connect failed for %s: %s", db_path, exc,
        )
        return None
    apply_workspace_pragmas(conn, readonly=True)
    return conn


def applied_schema_version_ro(
    db_path: Path,
    table: str,
    *,
    config: "PollyPMConfig | None" = None,
) -> int | None:
    """Return ``MAX(version)`` from a schema-version table, or ``None``.

    ``table`` is interpolated into the SQL string because the schema
    layer keeps two parallel version tables (``schema_version`` and
    ``work_schema_version``) and SQLite parameter binding does not
    support table identifiers. Callers pass the table name from a
    fixed set inside ``doctor.py`` — never user input.

    Returns ``None`` when the DB file is missing, the connect fails, or
    the table does not exist. Returns ``0`` when the table is present
    but empty (no migrations applied yet).

    On the pg backend both ``schema_version`` and ``work_schema_version``
    map to the unified ``schema_migrations`` table — the pg port collapses
    the two-version split into a single forward-only migration list
    (Slice A). Callers passing either table name get back the same
    ``MAX(version)`` so existing doctor checks keep working.
    """
    if is_pg_backend(config):
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

    conn = _connect_readonly(db_path)
    if conn is None:
        return None
    try:
        try:
            row = conn.execute(
                f"SELECT COALESCE(MAX(version), 0) FROM {table}"
            ).fetchone()
        except sqlite3.Error:
            return None
        return int(row[0]) if row and row[0] is not None else 0
    finally:
        conn.close()


def count_work_tasks_ro(
    db_path: Path,
    *,
    config: "PollyPMConfig | None" = None,
) -> int | None:
    """Return ``COUNT(*) FROM work_tasks`` on ``db_path``, or ``None``.

    Returns ``None`` when the file is missing OR when the ``work_tasks``
    table does not exist on it. Returns ``0`` when the table exists but
    is empty. Used by the dual-DB drift probe to tell "no work tables
    on this file" apart from "tables present but empty".
    """
    if is_pg_backend(config):
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

    conn = _connect_readonly(db_path)
    if conn is None:
        return None
    try:
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='work_tasks'"
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        try:
            row = conn.execute("SELECT COUNT(*) FROM work_tasks").fetchone()
        except sqlite3.Error:
            return None
        return int(row[0]) if row and row[0] is not None else 0
    finally:
        conn.close()


def has_messages_table_ro(
    db_path: Path,
    *,
    config: "PollyPMConfig | None" = None,
) -> bool:
    """Return ``True`` when ``db_path`` carries a ``messages`` table.

    Read-only probe used by the dual-DB drift check to decide whether a
    given state.db is the messages-side DB. Returns ``False`` on any
    open / read failure.
    """
    if is_pg_backend(config):
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

    conn = _connect_readonly(db_path)
    if conn is None:
        return False
    try:
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='messages'"
            ).fetchone()
        except sqlite3.Error:
            return False
        return row is not None
    finally:
        conn.close()


def sessions_row_count_ro(
    db_path: Path,
    *,
    config: "PollyPMConfig | None" = None,
) -> int | None:
    """Return ``COUNT(*) FROM sessions`` on ``db_path``, or ``None``.

    Returns ``None`` when the DB file cannot be opened or the
    ``sessions`` table does not exist (a fresh install before
    ``Supervisor.start()`` runs ``repair_sessions_table()``). Returns
    ``0`` when the table exists but is empty — the
    ``check_sessions_table_populated`` doctor check treats that as a
    distinct fail signal so the operator gets a clear "repair didn't
    run" hint instead of a silent skip.
    """
    if is_pg_backend(config):
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

    conn = _connect_readonly(db_path)
    if conn is None:
        return None
    try:
        try:
            row = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
        except sqlite3.Error:
            return None
        return int(row[0]) if row and row[0] is not None else 0
    finally:
        conn.close()


def session_window_names_ro(
    db_path: Path,
    *,
    config: "PollyPMConfig | None" = None,
) -> set[str] | None:
    """Return the set of ``window_name`` values in ``sessions``, or ``None``.

    Returns ``None`` when the DB cannot be opened or the ``sessions``
    table is missing — distinct from the empty-set case (table present,
    no rows) so the session-drift doctor check can skip cleanly when
    the table itself has not been provisioned yet.
    """
    if is_pg_backend(config):
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

    conn = _connect_readonly(db_path)
    if conn is None:
        return None
    try:
        windows = set()
        try:
            for row in conn.execute("SELECT window_name FROM sessions"):
                if row and row[0]:
                    windows.add(str(row[0]))
        except sqlite3.Error:
            return None
        return windows
    finally:
        conn.close()


__all__ = [
    "applied_schema_version_ro",
    "count_work_tasks_ro",
    "has_messages_table_ro",
    "sessions_row_count_ro",
    "session_window_names_ro",
]
