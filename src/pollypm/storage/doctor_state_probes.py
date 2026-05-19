"""Read-only state probes used by ``pm doctor``.

Following Slice K-state-callers-port (#1737) the probes prefer the
Postgres backend: when the process-wide RO pool is reachable, every
probe runs against pg. When the pool is unreachable or the install
hasn't migrated yet, the probes fall back to the sqlite file passed
via ``db_path`` so doctor checks against legacy / fixture DBs keep
working.

Every probe is:

* **read-only** — never mutates the DB;
* **best-effort** — pool / query / file failures degrade to the
  documented sentinel (``None``, ``False``, or ``0``) instead of
  propagating.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from pollypm.storage.sqlite_pragmas import apply_workspace_pragmas, readonly_uri

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


def _connect_readonly(db_path: Path) -> sqlite3.Connection | None:
    """Open ``db_path`` read-only or return ``None``.

    Returns ``None`` when the file does not exist or the connect call
    raises any ``sqlite3.Error``. Applies the workspace pragmas
    (``busy_timeout``) so concurrent writers don't starve the probe.
    """
    if not db_path.is_file():
        return None
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

    When ``db_path`` is a readable sqlite file we read from it (the
    table the caller named); otherwise we fall through to the pg
    ``schema_migrations`` table.
    """
    conn = _connect_readonly(db_path)
    if conn is not None:
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

    Reads the sqlite file at ``db_path`` when it's a regular file;
    otherwise falls through to the pg schema.
    """
    conn = _connect_readonly(db_path)
    if conn is not None:
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
    """Return ``True`` when the schema carries a ``messages`` table."""
    conn = _connect_readonly(db_path)
    if conn is not None:
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
    """Return ``COUNT(*) FROM sessions``, or ``None``."""
    conn = _connect_readonly(db_path)
    if conn is not None:
        try:
            try:
                row = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
            except sqlite3.Error:
                return None
            return int(row[0]) if row and row[0] is not None else 0
        finally:
            conn.close()

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
    """Return the set of ``window_name`` values in ``sessions``, or ``None``."""
    conn = _connect_readonly(db_path)
    if conn is not None:
        try:
            windows: set[str] = set()
            try:
                for row in conn.execute("SELECT window_name FROM sessions"):
                    if row and row[0]:
                        windows.add(str(row[0]))
            except sqlite3.Error:
                return None
            return windows
        finally:
            conn.close()

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
