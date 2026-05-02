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

#1021 follow-up
---------------
The first pass of #1018 only wrapped ``JobWorkerPool._run_one`` with
retry-on-lock. Three callers still propagated ``database is locked``
to the operator (``queue.enqueue``, ``queue.fail``, and
``HeartbeatRail.tick`` itself), which crashed the heartbeat ticker and
took every heartbeat-driven recovery surface offline. The shared
:func:`retry_on_database_locked` helper below collapses that retry
policy into one place so every write surface gets the same backoff
ladder, and :func:`is_database_locked_error` exposes the same
narrow-error predicate to non-worker callers.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Callable, TypeVar


# 5 s is the recommended floor for a multi-writer SQLite workload — long
# enough that a heartbeat-tick alert upsert can wait out a competing
# JobWorkerPool transaction, short enough that a genuinely wedged DB
# surfaces inside the @every 10 s sweep cadence rather than hanging the
# UI indefinitely. Tuned empirically against the #67108 reproducer.
DEFAULT_BUSY_TIMEOUT_MS = 5000

# #1018 — exponential backoff for the database-locked retry. Three
# attempts at 0.1 s / 0.5 s / 2.0 s (total ~2.6 s ceiling) gives the
# competing writer time to commit, while keeping the caller from
# stalling past the next @every 10 s sweep window. Mirrors
# ``pollypm.jobs.workers._DB_LOCK_RETRY_BACKOFF`` — both surfaces share
# this constant so behaviour stays consistent if tuning shifts.
DB_LOCK_RETRY_BACKOFF: tuple[float, ...] = (0.1, 0.5, 2.0)


logger = logging.getLogger(__name__)

T = TypeVar("T")


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


def is_database_locked_error(exc: BaseException) -> bool:
    """True iff ``exc`` is SQLite's transient ``database is locked``.

    The cockpit hits this whenever two writers race past the
    ``busy_timeout`` window. Different from ``ProgrammingError:
    Cannot operate on a closed database`` (#1006) — that one is
    permanent and there is nothing to retry. ``database is locked`` is
    ``OperationalError`` and means the operation has not been performed
    but the connection is still alive and the next attempt will likely
    succeed once the contending writer commits.

    Recognises both bare ``sqlite3.OperationalError`` and the wrapped
    SQLAlchemy ``OperationalError`` (DBAPI errors get re-raised inside
    ``sqlalchemy.exc.OperationalError`` with the original message
    appended in parentheses, which we still match by substring).

    The duplicate :func:`pollypm.jobs.workers._is_database_locked_error`
    is preserved for backwards compatibility with #1018; new callers
    should use this one and import it from ``pollypm.storage.sqlite_pragmas``.
    """
    msg = str(exc).lower()
    if "database is locked" not in msg and "database is busy" not in msg:
        return False
    # Constrain to OperationalError-flavoured ancestors — anything else
    # would be a stretch of the substring. ``sqlalchemy.exc.OperationalError``
    # is a different class hierarchy from ``sqlite3.OperationalError`` but
    # both expose ``.__class__.__name__ == "OperationalError"``.
    cls = exc.__class__
    if isinstance(exc, sqlite3.OperationalError):
        return True
    # Walk the class hierarchy looking for an ``OperationalError`` name —
    # avoids importing SQLAlchemy from this low-level helper.
    for base in cls.__mro__:
        if base.__name__ == "OperationalError":
            return True
    return False


def retry_on_database_locked(
    fn: Callable[[], T],
    *,
    backoff: tuple[float, ...] = DB_LOCK_RETRY_BACKOFF,
    label: str = "operation",
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run ``fn`` with retry-on-lock backoff.

    Retries up to ``len(backoff)`` times when ``fn`` raises a
    ``database is locked`` ``OperationalError``; sleeps ``backoff[i]``
    seconds between attempts. Any non-lock error propagates immediately;
    the lock error from the final attempt also propagates so callers
    can see the original traceback in the logs.

    ``label`` is included in the WARNING line emitted when a retry
    fires — it should be a short, callsite-identifying string
    (``"queue.enqueue"``, ``"HeartbeatRail.tick"``).

    ``sleep`` is injectable purely for tests — the production path
    uses ``time.sleep`` and pays the full backoff ladder. Because
    SQLite's lock-resolution path is millisecond-scale once WAL is on,
    the typical run never enters the retry branch at all.
    """
    attempts = 1 + len(backoff)
    for attempt_index in range(attempts):
        try:
            return fn()
        except BaseException as exc:  # noqa: BLE001
            if not is_database_locked_error(exc):
                raise
            if attempt_index >= len(backoff):
                # Exhausted — re-raise so the caller sees the live
                # error and can decide whether to escalate. Logged at
                # WARNING (not ERROR) so the heartbeat alert pipeline
                # does not promote a transient WAL contention to a
                # ``critical_error`` alert.
                logger.warning(
                    "%s: gave up after %d database-locked retries",
                    label, attempts,
                )
                raise
            delay = backoff[attempt_index]
            logger.debug(
                "%s: hit database-locked (attempt %d/%d); retrying after %.2fs",
                label, attempt_index + 1, attempts, delay,
            )
            sleep(delay)
    # Unreachable — the loop either returns or raises.
    raise AssertionError("retry_on_database_locked exhausted without raising")


__all__ = [
    "DB_LOCK_RETRY_BACKOFF",
    "DEFAULT_BUSY_TIMEOUT_MS",
    "apply_workspace_pragmas",
    "is_database_locked_error",
    "retry_on_database_locked",
]
