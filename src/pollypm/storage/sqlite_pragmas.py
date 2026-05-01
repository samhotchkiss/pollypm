"""Workspace SQLite connection PRAGMAs (#1018).

Post-#1004 collapsed all per-project DBs into a single workspace-root
``state.db``. The default rollback-journal mode serialises every writer,
so under heartbeat + JobWorkerPool load (4 workers x ``@every 10s``
``session.health_sweep`` + token-ledger sync + alert upserts) callers
hit ``sqlite3.OperationalError: database is locked`` mid-transaction,
which JobWorkerPool then escalates to a ``critical_error`` alert
(#67108).

Two cheap pragmas remove the contention:

* ``PRAGMA journal_mode=WAL`` lets readers run concurrently with a
  single writer instead of locking each other out. The setting is
  persistent — once a DB is in WAL mode it stays in WAL mode for every
  subsequent connection until an ``rollback`` re-issues the pragma.
  We re-issue it on every fresh connection anyway so newly-created
  DBs (tests, fresh installs) flip into WAL on first open.
* ``PRAGMA busy_timeout=5000`` tells SQLite to spin-wait up to 5s for
  a transient lock before raising ``database is locked``. Combined
  with WAL this collapses the lock window to milliseconds in practice.

The helper is deliberately tolerant — read-only ``file:...?mode=ro``
URIs reject ``journal_mode=WAL`` (the DB is immutable), so we swallow
that specific failure and still apply ``busy_timeout``.

Centralising here means future tuning (e.g. raising ``synchronous`` to
``NORMAL``, or wiring in ``wal_autocheckpoint``) lands at one site
rather than the >20 ``sqlite3.connect`` call-sites currently scattered
through the tree.
"""

from __future__ import annotations

import sqlite3

# 5 s is the recommended floor for a multi-writer SQLite workload — long
# enough that a heartbeat-tick alert upsert can wait out a competing
# JobWorkerPool transaction, short enough that a genuinely wedged DB
# surfaces inside the @every 10 s sweep cadence rather than hanging the
# UI indefinitely. Tuned empirically against the #67108 reproducer.
DEFAULT_BUSY_TIMEOUT_MS = 5000


def apply_workspace_pragmas(
    conn: sqlite3.Connection,
    *,
    readonly: bool = False,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> None:
    """Apply WAL + busy_timeout to ``conn``.

    Idempotent and best-effort: re-applying on a connection that already
    has WAL enabled is a no-op, and any pragma failure (e.g. running
    against a read-only URI that forbids ``journal_mode``) is swallowed
    so callers don't have to special-case the error path.

    ``readonly`` skips the ``journal_mode=WAL`` pragma — read-only URI
    connections (``file:db?mode=ro``) reject mode changes outright, and
    a separate writer is responsible for keeping the DB in WAL mode.
    ``busy_timeout`` is still applied: even read-only attaches block
    on writer checkpoints under heavy load.
    """
    try:
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    except sqlite3.Error:
        pass

    if readonly:
        return

    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        # Read-only attaches and ``mode=ro`` URIs raise here — the DB is
        # already in some journal mode set by an earlier writer, and we
        # don't need to flip it.
        pass


__all__ = ["DEFAULT_BUSY_TIMEOUT_MS", "apply_workspace_pragmas"]
