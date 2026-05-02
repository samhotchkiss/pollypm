"""SQLAlchemy engine factory — dual-pool writer/reader split for SQLite WAL.

Why dual pools: SQLite in WAL mode allows many concurrent readers but
serializes writers. Instead of fighting that with ``busy_timeout`` + retry
loops sprinkled through call sites, we expose it in the connection topology:

* **Writer engine** — ``QueuePool(pool_size=1, max_overflow=0)``. Every write
  transaction blocks on the single connection, so application code never sees
  "database is locked" under contention.
* **Reader engine** — ``QueuePool(pool_size=5)``. Multiple threads can read
  concurrently against the WAL snapshot without stepping on the writer.

SQLite pragmas (WAL, busy_timeout, synchronous=NORMAL, foreign_keys=ON) are
applied on every new connection via ``event.listens_for(engine, "connect")``.
:func:`is_sqlite` gates the pragma hook so the same factory will work against
a future Postgres URL without throwing "unknown pragma" errors on connect.

Issue #337 — foundation only. No schema, no table creation, no callers.
"""

from __future__ import annotations

import sqlite3
from urllib.parse import urlparse

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.pool import QueuePool

from pollypm.storage.sqlite_pragmas import apply_workspace_pragmas


# --------------------------------------------------------------------------
# Public helpers
# --------------------------------------------------------------------------


def is_sqlite(url: str) -> bool:
    """Return ``True`` if ``url`` is a SQLite SQLAlchemy URL.

    Used to gate the SQLite-specific pragma hook so :func:`make_engines`
    stays portable when PollyPM eventually grows a Postgres backend.

    Examples
    --------
    >>> is_sqlite("sqlite:///foo.db")
    True
    >>> is_sqlite("sqlite+pysqlite:///foo.db")
    True
    >>> is_sqlite("postgresql+psycopg://host/db")
    False
    """
    try:
        parsed = make_url(url)
    except Exception:
        # Fall back to the raw scheme if SQLAlchemy can't parse the URL.
        # We don't want a malformed URL here to explode — that's
        # ``create_engine``'s job to report downstream.
        scheme = urlparse(url).scheme.lower()
        return scheme.startswith("sqlite")
    return parsed.get_backend_name() == "sqlite"


def make_engines(url: str) -> tuple[Engine, Engine]:
    """Return ``(write_engine, read_engine)`` for ``url``.

    The writer serializes all writes on a single pooled connection; the
    reader pool allows up to 5 concurrent reads. For SQLite URLs, the
    WAL / busy_timeout / synchronous / foreign_keys pragmas are applied
    on every new connection.

    Parameters
    ----------
    url:
        SQLAlchemy URL. Typically ``sqlite:///<path>``; may also be
        ``sqlite:///:memory:`` for throwaway tests (but note memory DBs
        are per-connection, so the two engines will not share state).

    Returns
    -------
    (write_engine, read_engine)
        Two separate :class:`~sqlalchemy.engine.Engine` instances backed
        by independent ``QueuePool`` instances.
    """
    write_engine = create_engine(
        url,
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=30,
        future=True,
    )
    read_engine = create_engine(
        url,
        poolclass=QueuePool,
        pool_size=5,
        future=True,
    )

    if is_sqlite(url):
        _install_sqlite_pragmas(write_engine)
        _install_sqlite_pragmas(read_engine)

    return write_engine, read_engine


# --------------------------------------------------------------------------
# SQLite pragma hook
# --------------------------------------------------------------------------

# 30s busy_timeout for the SQLAlchemy pool: this engine fronts the heaviest
# writers (alert upserts, ``messages`` updates from the supervisor) and the
# 5s workspace default proved too tight under #1018 / #1021 contention.
_SQLALCHEMY_BUSY_TIMEOUT_MS = 30000

# Engine-only pragmas — :func:`apply_workspace_pragmas` covers
# ``journal_mode=WAL`` + ``busy_timeout``; we still need ``synchronous``
# and ``foreign_keys`` here.
_SQLITE_EXTRA_PRAGMAS: tuple[tuple[str, str], ...] = (
    ("synchronous", "NORMAL"),
    ("foreign_keys", "ON"),
)


def _install_sqlite_pragmas(engine: Engine) -> None:
    """Register a ``connect`` listener that applies PollyPM's SQLite pragmas.

    Called once per engine at construction time. SQLAlchemy invokes the
    listener on every new physical connection the pool opens, so reused
    pooled connections keep the pragmas they were initialized with.

    #1021 — routes through :func:`apply_workspace_pragmas` so the
    SQLAlchemy pool gets the same WAL + ``busy_timeout`` policy as the
    raw ``sqlite3.connect`` callers (StateStore, JobQueue, etc.). Pre-fix,
    #1018's helper only covered the stdlib path; the SQLAlchemy supervisor
    writers ran on a separately-tuned pragma set, and any drift between the
    two became a future-bug. ``synchronous`` and ``foreign_keys`` stay
    inline because they're SQLAlchemy-only knobs (the stdlib writers don't
    enforce FKs).
    """

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: object, _connection_record: object) -> None:
        # Only apply pragmas when the underlying DB-API is SQLite. If a
        # future Postgres dialect somehow reaches this hook (via a pool
        # configured with an unexpected URL), silently skip rather than
        # raising — the ``is_sqlite`` gate in :func:`make_engines` is the
        # primary guard; this is defense in depth.
        if not isinstance(dbapi_connection, sqlite3.Connection):
            return
        # Centralised WAL + busy_timeout (#1021) — same helper the
        # stdlib sqlite3.connect callers use.
        apply_workspace_pragmas(
            dbapi_connection, busy_timeout_ms=_SQLALCHEMY_BUSY_TIMEOUT_MS,
        )
        cursor = dbapi_connection.cursor()
        try:
            for name, value in _SQLITE_EXTRA_PRAGMAS:
                cursor.execute(f"PRAGMA {name}={value}")
        finally:
            cursor.close()
