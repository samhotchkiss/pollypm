"""Regression tests for #1018 — WAL + busy_timeout + lock retry.

The production symptom was alert ``#67108``::

    critical error_log/critical_error: JobWorkerPool: unexpected error
    running job 50926 (session.health_sweep): database is locked

Three orthogonal fixes:

* :func:`pollypm.storage.sqlite_pragmas.apply_workspace_pragmas` is the
  single connection-opener helper that flips every fresh writer
  connection into WAL mode and sets ``busy_timeout``. Tests below
  assert that ``StateStore``, ``JobQueue``, and ``SQLiteWorkService``
  all sit on a connection with WAL enabled (so concurrent readers no
  longer block writers, and a competing writer waits for ``busy_timeout``
  before raising).
* ``JobWorkerPool`` now retries handler invocations that raise
  ``sqlite3.OperationalError: database is locked`` with exponential
  backoff (0.1 s / 0.5 s / 2.0 s) before falling through to the regular
  ``fail()`` path. Critically, the retry path does NOT log via
  ``logger.exception`` — that's what was tripping the
  ``error_log/critical_error`` heartbeat alert.
* Concurrent writers no longer deadlock under the
  heartbeat + JobWorkerPool load that produced the live alert.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from pollypm.jobs import HandlerSpec, JobQueue, JobWorkerPool
from pollypm.jobs.workers import (
    _DB_LOCK_RETRY_BACKOFF,
    _is_database_locked_error,
)
from pollypm.storage.sqlite_pragmas import (
    DEFAULT_BUSY_TIMEOUT_MS,
    apply_workspace_pragmas,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _journal_mode(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA journal_mode").fetchone()
    return str(row[0]).lower() if row else ""


def _busy_timeout(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA busy_timeout").fetchone()
    return int(row[0]) if row else 0


def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ---------------------------------------------------------------------------
# Pragma helper — direct unit
# ---------------------------------------------------------------------------


def test_apply_workspace_pragmas_flips_into_wal(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    conn = sqlite3.connect(str(db))
    try:
        apply_workspace_pragmas(conn)
        assert _journal_mode(conn) == "wal"
        assert _busy_timeout(conn) == DEFAULT_BUSY_TIMEOUT_MS
    finally:
        conn.close()


def test_apply_workspace_pragmas_readonly_skips_journal_mode(tmp_path: Path) -> None:
    """Read-only URIs reject ``journal_mode`` writes — must not raise."""

    db = tmp_path / "ro.db"
    # Seed the file as a writable DB first so the read-only attach
    # has something to open.
    sqlite3.connect(str(db)).close()

    ro_conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        # No exception even though the connection is read-only.
        apply_workspace_pragmas(ro_conn, readonly=True)
        # busy_timeout still applies (lets readers wait out a writer).
        assert _busy_timeout(ro_conn) == DEFAULT_BUSY_TIMEOUT_MS
    finally:
        ro_conn.close()


# ---------------------------------------------------------------------------
# Per-call-site WAL assertions — the reason the helper exists
# ---------------------------------------------------------------------------


def test_jobqueue_owns_connection_in_wal_mode(tmp_path: Path) -> None:
    """JobQueue (one of the heaviest writers) must open in WAL."""

    q = JobQueue(db_path=tmp_path / "jobs.db")
    try:
        with q._lock:
            assert _journal_mode(q._conn) == "wal"
            # JobQueue keeps the historical 30 s window for heavy
            # claim/complete bursts (see #1018 commit message).
            assert _busy_timeout(q._conn) >= DEFAULT_BUSY_TIMEOUT_MS
    finally:
        q.close()


def test_statestore_writes_in_wal_mode(tmp_path: Path) -> None:
    """StateStore is the alert-upsert writer; must be in WAL."""

    from pollypm.storage.state import StateStore

    store = StateStore(tmp_path / "state.db")
    try:
        with store._lock:
            assert _journal_mode(store._conn) == "wal"
            assert _busy_timeout(store._conn) >= DEFAULT_BUSY_TIMEOUT_MS
    finally:
        store.close()


def test_sqlite_work_service_writes_in_wal_mode(tmp_path: Path) -> None:
    """SQLiteWorkService is the work-tasks writer; must be in WAL."""

    from pollypm.work.sqlite_service import SQLiteWorkService

    svc = SQLiteWorkService(tmp_path / "work.db")
    try:
        assert _journal_mode(svc._conn) == "wal"
        assert _busy_timeout(svc._conn) >= DEFAULT_BUSY_TIMEOUT_MS
    finally:
        svc.close()


# ---------------------------------------------------------------------------
# Concurrent-writer regression — the live failure mode for #67108
# ---------------------------------------------------------------------------


def test_concurrent_writers_do_not_propagate_database_locked(tmp_path: Path) -> None:
    """Heartbeat + JobWorkerPool style write contention does not crash.

    Pre-fix, two writers on the same DB without WAL would serialize and
    the second writer's commit would raise
    ``sqlite3.OperationalError: database is locked`` past the default
    rollback-journal timeout. With WAL + busy_timeout=5000 in place
    (and the JobWorkerPool retry-on-lock as belt-and-braces), the
    writers should all complete without the error escaping.

    We don't try to provoke a *guaranteed* lock — that depends on
    SQLite's WAL checkpoint timing. Instead we hammer two writer
    threads and assert no ``database is locked`` reaches the caller.
    """

    db_path = tmp_path / "shared.db"

    # Bootstrap the shared schema via a real workspace writer so both
    # threads see the same table layout the production code uses.
    bootstrap = sqlite3.connect(str(db_path))
    apply_workspace_pragmas(bootstrap)
    bootstrap.execute(
        "CREATE TABLE IF NOT EXISTS contention "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, who TEXT, n INTEGER)"
    )
    bootstrap.commit()
    bootstrap.close()

    errors: list[BaseException] = []
    write_count = {"a": 0, "b": 0}

    def writer(label: str, iters: int) -> None:
        try:
            conn = sqlite3.connect(str(db_path), timeout=10.0)
            apply_workspace_pragmas(conn)
            try:
                for i in range(iters):
                    conn.execute(
                        "INSERT INTO contention (who, n) VALUES (?, ?)",
                        (label, i),
                    )
                    conn.commit()
                    write_count[label] += 1
            finally:
                conn.close()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t_a = threading.Thread(target=writer, args=("a", 200))
    t_b = threading.Thread(target=writer, args=("b", 200))
    t_a.start()
    t_b.start()
    t_a.join(timeout=15.0)
    t_b.join(timeout=15.0)

    assert not t_a.is_alive() and not t_b.is_alive(), "writer thread hung"
    assert not errors, f"unexpected errors: {errors!r}"
    assert write_count == {"a": 200, "b": 200}


# ---------------------------------------------------------------------------
# JobWorkerPool retry-on-lock — direct unit
# ---------------------------------------------------------------------------


def test_is_database_locked_error_recognises_operational_error() -> None:
    locked = sqlite3.OperationalError("database is locked")
    busy = sqlite3.OperationalError("database is busy")
    other_op = sqlite3.OperationalError("near 'WHERE': syntax error")
    closed = sqlite3.ProgrammingError("Cannot operate on a closed database")

    assert _is_database_locked_error(locked) is True
    assert _is_database_locked_error(busy) is True
    assert _is_database_locked_error(other_op) is False
    assert _is_database_locked_error(closed) is False


def test_pool_retries_handler_when_first_call_raises_database_locked(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """First-attempt lock retries; second attempt succeeds; no critical log.

    Reproduces the ``#67108`` symptom in miniature: a handler that
    raises ``sqlite3.OperationalError: database is locked`` on its
    first invocation but succeeds on the next. Pre-fix, this would
    have been logged via ``logger.exception`` (which the heartbeat
    alert pipeline escalates to ``critical_error``). Post-fix the
    handler is retried and the job completes cleanly.
    """

    from pollypm.jobs import exponential_backoff

    q = JobQueue(
        db_path=tmp_path / "jobs.db",
        retry_policy=exponential_backoff(
            base_seconds=0.01, factor=1.0, max_seconds=0.01, jitter=0,
        ),
    )

    invocations = {"n": 0}

    def flaky(payload: dict) -> None:
        invocations["n"] += 1
        if invocations["n"] == 1:
            raise sqlite3.OperationalError("database is locked")

    registry = {"flaky": HandlerSpec("flaky", flaky, timeout_seconds=2)}
    # Patch the retry backoff to something near-instant so the test
    # doesn't pay the production 0.1 / 0.5 / 2.0 s ladder.
    import pollypm.jobs.workers as _workers

    original = _workers._DB_LOCK_RETRY_BACKOFF
    _workers._DB_LOCK_RETRY_BACKOFF = (0.01, 0.01, 0.01)
    try:
        pool = JobWorkerPool(q, registry=registry, poll_interval=0.01)
        pool.start(concurrency=1)
        try:
            jid = q.enqueue("flaky", max_attempts=1)
            assert _wait_until(lambda: q.stats().done == 1, timeout=5.0), (
                f"job did not complete; stats={q.stats()!r} "
                f"invocations={invocations!r}"
            )
        finally:
            pool.stop(timeout=2)
    finally:
        _workers._DB_LOCK_RETRY_BACKOFF = original

    assert invocations["n"] == 2, "handler not retried after database-locked"

    # Critical: the lock retry must NOT log via logger.exception.
    # That's how the production traceback became a ``critical_error``
    # alert in the first place.
    exception_records = [
        r for r in caplog.records
        if r.levelname == "ERROR" and "database is locked" in r.getMessage()
    ]
    assert not exception_records, (
        "lock retry escalated to ERROR-level log "
        f"(would surface as critical_error alert): {exception_records!r}"
    )


def test_pool_gives_up_after_exhausting_lock_retries(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """A handler that *keeps* raising locked falls through to fail()."""

    from pollypm.jobs import exponential_backoff

    q = JobQueue(
        db_path=tmp_path / "jobs.db",
        retry_policy=exponential_backoff(
            base_seconds=0.01, factor=1.0, max_seconds=0.01, jitter=0,
        ),
    )

    invocations = {"n": 0}

    def stuck(payload: dict) -> None:
        invocations["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    registry = {"stuck": HandlerSpec("stuck", stuck, timeout_seconds=2, max_attempts=1)}

    import pollypm.jobs.workers as _workers

    original = _workers._DB_LOCK_RETRY_BACKOFF
    _workers._DB_LOCK_RETRY_BACKOFF = (0.01, 0.01, 0.01)
    try:
        pool = JobWorkerPool(q, registry=registry, poll_interval=0.01)
        pool.start(concurrency=1)
        try:
            q.enqueue("stuck", max_attempts=1)
            assert _wait_until(lambda: q.stats().failed == 1, timeout=5.0)
        finally:
            pool.stop(timeout=2)
    finally:
        _workers._DB_LOCK_RETRY_BACKOFF = original

    # 1 initial + len(_DB_LOCK_RETRY_BACKOFF) retries
    assert invocations["n"] == 1 + len(original)
