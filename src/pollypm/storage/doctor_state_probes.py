"""Read-only state probes used by ``pm doctor``.

Following Slice K-state-callers-port (#1737) the probes prefer the
Postgres backend: when the caller supplies a config whose active backend
is ``"postgres"``, the probes go straight to pg via the process-wide RO
pool. When the active backend is sqlite (or no config is in scope), the
probes fall back to the sqlite file passed via ``db_path`` so doctor
checks against legacy / fixture DBs keep working.

#1856: previously every probe read the sqlite file *first* whenever the
file happened to exist on disk — even when ``[storage] backend =
"postgres"`` was set. A stale ``~/.pollypm/state.db`` from a pre-cutover
install would mask the live pg state and make the doctor report read
from the wrong source. Backend dispatch now runs before the sqlite probe
whenever a ``config`` is in scope. (Callers that don't pass a ``config``
keep the legacy sqlite-first path so test harnesses that construct fake
db_paths without booting a config don't accidentally hit the user's
real pg pool.)

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

from pollypm.storage._backend_dispatch import is_pg_backend
from pollypm.storage.sqlite_pragmas import apply_workspace_pragmas, readonly_uri

if TYPE_CHECKING:
    from pollypm.models import PollyPMConfig

logger = logging.getLogger(__name__)


def _should_route_to_pg(config: "PollyPMConfig | None") -> bool:
    """Return True when this call should bypass sqlite and read pg.

    Only diverts when an explicit ``config`` was supplied AND its
    storage backend is postgres. A ``None`` config preserves the legacy
    sqlite-first behaviour so callers that don't thread a config through
    (older test harnesses, anything that hand-builds a ``db_path`` for a
    fixture) keep working without picking up the user's process-wide
    real config.
    """
    if config is None:
        return False
    return is_pg_backend(config)


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

    When the active backend is postgres we read ``schema_migrations`` via
    the RO pool. Otherwise we read the sqlite file at ``db_path``.
    """
    if _should_route_to_pg(config):
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
    """Return ``COUNT(*) FROM work_tasks``, or ``None``.

    When the active backend is postgres we query the pg schema; otherwise
    we read the sqlite file at ``db_path``.
    """
    if _should_route_to_pg(config):
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
    """Return ``True`` when the schema carries a ``messages`` table."""
    if _should_route_to_pg(config):
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
    """Return ``COUNT(*) FROM sessions``, or ``None``."""
    if _should_route_to_pg(config):
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
    """Return the set of ``window_name`` values in ``sessions``, or ``None``."""
    if _should_route_to_pg(config):
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


__all__ = [
    "applied_schema_version_ro",
    "count_work_tasks_ro",
    "has_messages_table_ro",
    "sessions_row_count_ro",
    "session_window_names_ro",
]
