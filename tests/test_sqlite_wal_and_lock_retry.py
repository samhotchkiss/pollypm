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


# ---------------------------------------------------------------------------
# #1021 — shared retry helper + queue.enqueue / queue.fail / tick coverage
# ---------------------------------------------------------------------------


def test_is_database_locked_error_helper_in_sqlite_pragmas() -> None:
    """Public predicate exposed alongside ``apply_workspace_pragmas``."""

    from pollypm.storage.sqlite_pragmas import is_database_locked_error

    assert is_database_locked_error(sqlite3.OperationalError("database is locked"))
    assert is_database_locked_error(sqlite3.OperationalError("database is busy"))
    assert not is_database_locked_error(sqlite3.OperationalError("syntax error"))
    assert not is_database_locked_error(
        sqlite3.ProgrammingError("Cannot operate on a closed database")
    )

    # SQLAlchemy-style wrapper detection: a fake exception class named
    # ``OperationalError`` should match by class name (the real wrapper
    # lives in ``sqlalchemy.exc`` and we don't want to import it from
    # the low-level helper).
    class OperationalError(Exception):
        pass

    assert is_database_locked_error(OperationalError("(...) database is locked (...)"))


def test_retry_on_database_locked_succeeds_after_one_lock(monkeypatch) -> None:
    from pollypm.storage import sqlite_pragmas as sp

    calls = {"n": 0}
    sleeps: list[float] = []

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    result = sp.retry_on_database_locked(
        flaky, label="test", sleep=sleeps.append,
    )
    assert result == "ok"
    assert calls["n"] == 2
    # First retry uses backoff[0].
    assert sleeps == [sp.DB_LOCK_RETRY_BACKOFF[0]]


def test_retry_on_database_locked_propagates_non_lock_errors() -> None:
    from pollypm.storage import sqlite_pragmas as sp

    def boom() -> None:
        raise sqlite3.OperationalError("syntax error")

    with pytest.raises(sqlite3.OperationalError, match="syntax error"):
        sp.retry_on_database_locked(boom, label="test", sleep=lambda _s: None)


def test_retry_on_database_locked_exhausts_then_raises() -> None:
    from pollypm.storage import sqlite_pragmas as sp

    calls = {"n": 0}

    def stuck() -> None:
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        sp.retry_on_database_locked(stuck, label="test", sleep=lambda _s: None)

    # 1 initial + 3 retries = 4 attempts.
    assert calls["n"] == 1 + len(sp.DB_LOCK_RETRY_BACKOFF)


class _OneShotLockingConnection:
    """Wrapper that injects a single ``database is locked`` on a matched SQL prefix.

    Wrapping the connection lets us proxy through every method JobQueue
    uses (``execute``, ``executescript``, ``commit``, ``__enter__``, etc.)
    while only intercepting the first ``execute`` whose SQL starts with
    a target verb. ``sqlite3.Connection`` exposes its slots read-only,
    so we can't monkey-patch ``execute`` directly.
    """

    def __init__(self, conn: sqlite3.Connection, target_prefix: str) -> None:
        self._conn = conn
        self._target = target_prefix.upper()
        self.fired = False

    def execute(self, sql, *args, **kwargs):
        if not self.fired and sql.lstrip().upper().startswith(self._target):
            self.fired = True
            raise sqlite3.OperationalError("database is locked")
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        # Anything we don't override (executescript, close, commit, ...)
        # passes straight through to the wrapped connection.
        return getattr(self._conn, name)


def test_queue_enqueue_retries_on_database_locked(tmp_path: Path) -> None:
    """``queue.enqueue`` must retry transient locks (#1021 path 1)."""

    from pollypm.storage import sqlite_pragmas as sp

    q = JobQueue(db_path=tmp_path / "jobs.db")
    try:
        wrapper = _OneShotLockingConnection(q._conn, "INSERT")
        original_conn = q._conn
        original_backoff = sp.DB_LOCK_RETRY_BACKOFF
        sp.DB_LOCK_RETRY_BACKOFF = (0.001, 0.001, 0.001)
        q._conn = wrapper  # type: ignore[assignment]
        try:
            jid = q.enqueue("noop", {"x": 1})
        finally:
            q._conn = original_conn
            sp.DB_LOCK_RETRY_BACKOFF = original_backoff

        assert isinstance(jid, int) and jid > 0
        assert wrapper.fired, "test did not trigger the lock branch"
        # Job actually landed in the table.
        assert q.stats().queued == 1
    finally:
        q.close()


def test_queue_fail_retries_on_database_locked(tmp_path: Path) -> None:
    """``queue.fail`` must retry transient locks (#1021 path 2)."""

    from pollypm.storage import sqlite_pragmas as sp

    q = JobQueue(db_path=tmp_path / "jobs.db")
    try:
        # Real job to fail.
        jid = q.enqueue("noop")
        # Move it to claimed so ``fail()`` follows the realistic path.
        q.claim("worker-test", limit=1)

        wrapper = _OneShotLockingConnection(q._conn, "UPDATE")
        original_conn = q._conn
        original_backoff = sp.DB_LOCK_RETRY_BACKOFF
        sp.DB_LOCK_RETRY_BACKOFF = (0.001, 0.001, 0.001)
        q._conn = wrapper  # type: ignore[assignment]
        try:
            q.fail(jid, "boom", retry=False)
        finally:
            q._conn = original_conn
            sp.DB_LOCK_RETRY_BACKOFF = original_backoff

        assert wrapper.fired, "test did not trigger the lock branch"
        # Failure landed: job is in failed state.
        assert q.stats().failed == 1
    finally:
        q.close()


def test_heartbeatrail_tick_swallows_transient_lock(tmp_path: Path) -> None:
    """``HeartbeatRail.tick`` retries on lock (#1021 path 3 — defense-in-depth).

    Builds a minimal ``HeartbeatRail`` and stubs the inner ``Heartbeat.tick``
    to raise ``database is locked`` once. The wrapper should retry, succeed
    on the second attempt, and never raise.
    """

    from pollypm.heartbeat.boot import HeartbeatRail
    from pollypm.storage import sqlite_pragmas as sp

    # Construct a HeartbeatRail without going through ``from_config`` —
    # we just need the ``tick`` method, not a live worker pool.
    rail = HeartbeatRail.__new__(HeartbeatRail)

    calls = {"n": 0}

    class _StubHeartbeat:
        def tick(self, _now):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return "tick-result"

    rail.heartbeat = _StubHeartbeat()  # type: ignore[attr-defined]

    original_backoff = sp.DB_LOCK_RETRY_BACKOFF
    sp.DB_LOCK_RETRY_BACKOFF = (0.001, 0.001, 0.001)
    try:
        result = rail.tick()
    finally:
        sp.DB_LOCK_RETRY_BACKOFF = original_backoff

    assert result == "tick-result"
    assert calls["n"] == 2


def test_sqlalchemy_engine_applies_wal_and_busy_timeout(tmp_path: Path) -> None:
    """Engine pool's ``connect`` listener routes through ``apply_workspace_pragmas``.

    #1021 path 3 — ensures the SQLAlchemy supervisor writer pool gets WAL +
    busy_timeout on every fresh pooled connection. Pre-#1021, the engine
    applied pragmas inline; post-#1021 it routes through the shared helper
    so the pool stays in lockstep with the stdlib ``sqlite3.connect`` callers.
    """

    from pollypm.store.engine import make_engines

    db = tmp_path / "messages.db"
    write_engine, read_engine = make_engines(f"sqlite:///{db}")
    try:
        with write_engine.connect() as conn:
            mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
            timeout = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()
        assert str(mode).lower() == "wal"
        assert int(timeout) >= DEFAULT_BUSY_TIMEOUT_MS

        with read_engine.connect() as conn:
            timeout = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()
        assert int(timeout) >= DEFAULT_BUSY_TIMEOUT_MS
    finally:
        write_engine.dispose()
        read_engine.dispose()
