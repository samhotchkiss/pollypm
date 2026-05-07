"""Unit tests for the job worker pool."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from pollypm.jobs import (
    HandlerSpec,
    JobQueue,
    JobStatus,
    JobWorkerPool,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _make_queue(tmp_path: Path) -> JobQueue:
    # Use a simple no-jitter exponential so tests are deterministic.
    from pollypm.jobs import exponential_backoff

    return JobQueue(
        db_path=tmp_path / "jobs.db",
        retry_policy=exponential_backoff(base_seconds=0.01, factor=1.0, max_seconds=0.01, jitter=0),
    )


# ---------------------------------------------------------------------------
# Basic success / failure
# ---------------------------------------------------------------------------


def test_successful_handler_marks_job_done(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    seen: list[dict] = []

    def handler(payload: dict) -> None:
        seen.append(payload)

    registry = {"h": HandlerSpec("h", handler, timeout_seconds=5)}
    pool = JobWorkerPool(q, registry=registry, poll_interval=0.01)
    pool.start(concurrency=2)
    try:
        q.enqueue("h", {"x": 1})
        assert _wait_until(lambda: q.stats().done == 1)
    finally:
        pool.stop(timeout=2)

    assert seen == [{"x": 1}]
    metrics = pool.metrics.snapshot()
    assert metrics["h"]["jobs_completed"] == 1
    assert metrics["h"]["jobs_failed"] == 0


def test_handler_exception_fails_with_retry(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)

    def boom(payload: dict) -> None:
        raise RuntimeError("nope")

    # max_attempts=1 means the first failure is terminal.
    registry = {"boom": HandlerSpec("boom", boom, timeout_seconds=5, max_attempts=1)}
    pool = JobWorkerPool(q, registry=registry, poll_interval=0.01)
    pool.start(concurrency=1)
    try:
        jid = q.enqueue("boom", max_attempts=1)
        assert _wait_until(lambda: q.stats().failed == 1, timeout=3.0)
    finally:
        pool.stop(timeout=2)

    stored = q.get(jid)
    assert stored is not None
    assert stored.status is JobStatus.FAILED
    last_error = q.get_last_error(jid) or ""
    assert "RuntimeError" in last_error
    assert "nope" in last_error


def test_handler_exception_retries_until_exhausted(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    attempts = {"count": 0}

    def flaky(payload: dict) -> None:
        attempts["count"] += 1
        raise RuntimeError("keep failing")

    registry = {"flaky": HandlerSpec("flaky", flaky, timeout_seconds=5, max_attempts=3)}
    pool = JobWorkerPool(q, registry=registry, poll_interval=0.01)
    pool.start(concurrency=1)
    try:
        jid = q.enqueue("flaky", max_attempts=3)
        assert _wait_until(lambda: q.stats().failed == 1, timeout=5.0)
    finally:
        pool.stop(timeout=2)

    assert attempts["count"] == 3
    stored = q.get(jid)
    assert stored is not None
    assert stored.status is JobStatus.FAILED


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


def test_handler_timeout_fails_job(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)

    def slow(payload: dict) -> None:
        # Sleep much longer than the handler's declared timeout.
        time.sleep(5)

    # timeout 0.1s, max_attempts 1 so first timeout is terminal.
    registry = {"slow": HandlerSpec("slow", slow, timeout_seconds=0.1, max_attempts=1)}
    pool = JobWorkerPool(q, registry=registry, poll_interval=0.01)
    pool.start(concurrency=1)
    try:
        jid = q.enqueue("slow", max_attempts=1)
        assert _wait_until(lambda: q.stats().failed == 1, timeout=3.0)
    finally:
        pool.stop(timeout=2)

    stored = q.get(jid)
    assert stored is not None
    assert stored.status is JobStatus.FAILED
    last_error = q.get_last_error(jid) or ""
    assert "timeout" in last_error.lower()


# ---------------------------------------------------------------------------
# Unknown handler
# ---------------------------------------------------------------------------


def test_unknown_handler_fails_permanently(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)

    pool = JobWorkerPool(q, registry={}, poll_interval=0.01)
    pool.start(concurrency=1)
    try:
        jid = q.enqueue("nobody-home")
        assert _wait_until(lambda: q.stats().failed == 1, timeout=3.0)
    finally:
        pool.stop(timeout=2)

    stored = q.get(jid)
    assert stored is not None
    assert stored.status is JobStatus.FAILED
    last_error = q.get_last_error(jid) or ""
    assert "nobody-home" in last_error


# ---------------------------------------------------------------------------
# Isolation: one failing handler doesn't block others
# ---------------------------------------------------------------------------


def test_failing_handler_does_not_wedge_others(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    successes: list[int] = []

    def bad(payload: dict) -> None:
        raise RuntimeError("bad")

    def good(payload: dict) -> None:
        successes.append(payload["i"])

    registry = {
        "bad": HandlerSpec("bad", bad, timeout_seconds=5, max_attempts=1),
        "good": HandlerSpec("good", good, timeout_seconds=5),
    }
    pool = JobWorkerPool(q, registry=registry, poll_interval=0.01)
    pool.start(concurrency=2)
    try:
        for i in range(5):
            q.enqueue("bad", max_attempts=1)
        for i in range(10):
            q.enqueue("good", {"i": i})

        assert _wait_until(
            lambda: q.stats().done == 10 and q.stats().failed == 5,
            timeout=5.0,
        )
    finally:
        pool.stop(timeout=2)

    assert sorted(successes) == list(range(10))


# ---------------------------------------------------------------------------
# Concurrency: pool drains faster than serial
# ---------------------------------------------------------------------------


def test_pool_drains_queue_concurrently(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)

    def sleepy(payload: dict) -> None:
        time.sleep(0.1)

    registry = {"s": HandlerSpec("s", sleepy, timeout_seconds=5)}
    # 10 sleepy jobs × 100ms serial = ~1s. With concurrency=5 should be ~200ms.
    for _ in range(10):
        q.enqueue("s")

    pool = JobWorkerPool(q, registry=registry, poll_interval=0.01)
    start = time.monotonic()
    pool.start(concurrency=5)
    try:
        assert _wait_until(lambda: q.stats().done == 10, timeout=3.0)
    finally:
        pool.stop(timeout=2)
    elapsed = time.monotonic() - start

    # Allow generous slack for CI jitter but still prove parallelism.
    assert elapsed < 0.8, f"pool took {elapsed:.3f}s (expected concurrency speedup)"


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


def test_stop_is_idempotent(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    pool = JobWorkerPool(q, registry={}, poll_interval=0.01)
    pool.start(concurrency=1)
    pool.stop(timeout=1)
    pool.stop(timeout=1)  # second stop shouldn't error


def test_double_start_raises(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    pool = JobWorkerPool(q, registry={}, poll_interval=0.01)
    pool.start(concurrency=1)
    try:
        with pytest.raises(RuntimeError):
            pool.start(concurrency=1)
    finally:
        pool.stop(timeout=1)


def test_concurrency_must_be_positive(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    pool = JobWorkerPool(q, registry={}, poll_interval=0.01)
    with pytest.raises(ValueError):
        pool.start(concurrency=0)
    with pytest.raises(ValueError):
        pool.start(concurrency=-1)


def test_stop_waits_for_in_flight_job(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    release = threading.Event()
    started = threading.Event()
    completed = threading.Event()

    def waiter(payload: dict) -> None:
        started.set()
        release.wait(timeout=2)
        completed.set()

    registry = {"w": HandlerSpec("w", waiter, timeout_seconds=5)}
    pool = JobWorkerPool(q, registry=registry, poll_interval=0.01)
    pool.start(concurrency=1)
    q.enqueue("w")

    assert started.wait(timeout=2), "handler never started"

    # stop() triggers in parallel; release the handler so it finishes cleanly.
    def run_stop() -> None:
        pool.stop(timeout=3)

    stop_thread = threading.Thread(target=run_stop)
    stop_thread.start()
    time.sleep(0.1)  # give stop() time to signal
    release.set()
    stop_thread.join(timeout=3)
    assert not stop_thread.is_alive()
    assert completed.is_set()


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_pool_drains_when_db_connection_closed_under_workers(tmp_path: Path) -> None:
    """#1006: closed-DB errors must not zombie the pool.

    Reproduces the production death sequence: ``pm migrate --apply``
    archives a per-project DB the cockpit's ``JobQueue`` was still
    pointing at. The next ``complete()`` / ``claim()`` raises
    ``sqlite3.ProgrammingError: Cannot operate on a closed database``.
    Pre-fix, every worker thread tight-looped that traceback into
    ``errors.log`` until ``pool.stop()``'s join timeout lapsed and
    rail_daemon was zombied. The fix trips the stop event so all
    workers exit on their next short-poll.

    We trigger the failure by acquiring the queue's lock, closing the
    underlying connection, and releasing — racing a bare
    ``conn.close()`` against an in-flight ``execute()`` is undefined
    behaviour in Python 3.14's sqlite3 binding (segfaults), so we go
    through the lock the queue itself uses to serialize access.
    """
    import sqlite3 as _sqlite3

    db_path = tmp_path / "jobs.db"
    conn = _sqlite3.connect(
        str(db_path), check_same_thread=False, isolation_level=None,
    )
    conn.execute("PRAGMA journal_mode=WAL")
    q = JobQueue(connection=conn)

    drained = threading.Event()
    enqueue_lock = threading.Lock()
    completed = [0]

    def quick(payload: dict) -> None:
        # No-op handler — finishes fast so the worker reaches complete().
        with enqueue_lock:
            completed[0] += 1
            if completed[0] >= 1:
                drained.set()

    registry = {"h": HandlerSpec("h", quick, timeout_seconds=5)}
    pool = JobWorkerPool(q, registry=registry, poll_interval=0.02)
    pool.start(concurrency=4)
    try:
        # Push enough work that workers are actively polling, then wait
        # for at least one to drain so we know the pool is live.
        for _ in range(2):
            q.enqueue("h")
        assert drained.wait(timeout=3), "no job ever completed"

        # Yank the connection through the queue's own lock — this is
        # what production hits when a sibling closes the connection
        # under the pool. No race against an in-flight execute().
        with q._lock:
            q._conn.close()

        # Ask for stop. The fix is: workers detect closed-DB on their
        # next claim() and bail out fast. ``stop()`` returning quickly
        # *and* every worker thread being gone is the success signal.
        t0 = time.monotonic()
        pool.stop(timeout=3.0)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.5, (
            f"pool.stop took {elapsed:.2f}s — workers tight-looped on "
            "closed-DB instead of exiting cleanly"
        )
    finally:
        # Defensive: don't leave threads behind if assertions fail.
        pool.stop(timeout=1.0)

    # Only count main pool worker threads (``pollypm-jobworker-N``),
    # not the per-handler invocation threads (``...-handler-N``) that
    # the pool spawns daemon-style and never joins on stop. The
    # production failure mode was the *worker* threads zombying.
    alive = [
        t for t in threading.enumerate()
        if t.name.startswith("pollypm-jobworker-")
        and "handler" not in t.name
    ]
    assert not alive, f"workers still alive after stop: {[t.name for t in alive]}"


def test_metrics_track_per_handler_counts_and_duration(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)

    def a(payload: dict) -> None:
        time.sleep(0.01)

    def b(payload: dict) -> None:
        raise RuntimeError("err")

    registry = {
        "a": HandlerSpec("a", a, timeout_seconds=5),
        "b": HandlerSpec("b", b, timeout_seconds=5, max_attempts=1),
    }
    pool = JobWorkerPool(q, registry=registry, poll_interval=0.01)
    pool.start(concurrency=1)
    try:
        for _ in range(3):
            q.enqueue("a")
        q.enqueue("b", max_attempts=1)
        assert _wait_until(
            lambda: q.stats().done == 3 and q.stats().failed == 1,
            timeout=3.0,
        )
    finally:
        pool.stop(timeout=2)

    snapshot = pool.metrics.snapshot()
    assert snapshot["a"]["jobs_completed"] == 3
    assert snapshot["a"]["jobs_failed"] == 0
    assert snapshot["a"]["avg_duration_ms"] > 0
    assert snapshot["b"]["jobs_completed"] == 0
    assert snapshot["b"]["jobs_failed"] == 1


# ---------------------------------------------------------------------------
# #1370 — JobWorkerPool thread leak regression coverage
# ---------------------------------------------------------------------------


def _handler_thread_count(prefix: str) -> int:
    """Count live threads whose name starts with ``<prefix>-handler``.

    Pre-fix, ``_run_one`` spawned one such thread per job *attempt*
    and abandoned it on timeout, so the count grew monotonically.
    Post-fix the executor reuses a single handler thread per worker.
    """
    return sum(
        1 for t in threading.enumerate()
        if t.name.startswith(f"{prefix}-handler") and t.is_alive()
    )


def test_handler_thread_does_not_leak_per_attempt(tmp_path: Path) -> None:
    """#1370: handler invocations must reuse one thread per worker.

    Pre-fix, ``_run_one`` spawned a fresh ``threading.Thread`` for
    every retry attempt and abandoned the reference on the timeout
    path. With the lock-retry loop active that meant up to 4 threads
    per job, all daemon, all leaked for the lifetime of the process.
    The fix routes invocations through a single-slot
    ``ThreadPoolExecutor`` per worker, so the live handler-thread
    count stays bounded by ``concurrency`` no matter how many jobs
    run.
    """
    q = _make_queue(tmp_path)

    def quick(payload: dict) -> None:
        return None

    prefix = "leak-test-pool"
    registry = {"q": HandlerSpec("q", quick, timeout_seconds=5)}
    pool = JobWorkerPool(
        q, registry=registry, poll_interval=0.01,
        worker_name_prefix=prefix,
    )
    pool.start(concurrency=2)
    try:
        for _ in range(50):
            q.enqueue("q")
        assert _wait_until(lambda: q.stats().done == 50, timeout=10.0)
        # With concurrency=2 and the executor-based fix, at most two
        # handler threads should ever be alive concurrently. Pre-fix
        # this would be ~50 (one per completed job, since daemon
        # threads outlive their invocation by epsilon under load).
        live = _handler_thread_count(prefix)
        assert live <= 2, f"handler threads leaked: {live} alive"
    finally:
        pool.stop(timeout=2)


def test_timed_out_handler_does_not_spawn_new_thread_each_retry(
    tmp_path: Path,
) -> None:
    """#1370: a hung handler must not multiply handler threads.

    Pre-fix, every lock-retry attempt that timed out left a daemon
    thread running the original handler. The post-fix executor
    serializes attempts on a single slot — once a handler hangs past
    its timeout we stop submitting new attempts (the executor would
    block on the same slot anyway). Verifies the count stays at most
    ``concurrency`` even when every handler hangs.
    """
    q = _make_queue(tmp_path)
    release = threading.Event()

    def hang(payload: dict) -> None:
        # Block until the test releases us — must not be killed.
        release.wait(timeout=10)

    prefix = "hang-test-pool"
    registry = {"hang": HandlerSpec(
        "hang", hang, timeout_seconds=0.05, max_attempts=1,
    )}
    pool = JobWorkerPool(
        q, registry=registry, poll_interval=0.01,
        worker_name_prefix=prefix,
    )
    pool.start(concurrency=1)
    try:
        for _ in range(5):
            q.enqueue("hang", max_attempts=1)
        # Each job times out at 50ms; with 5 jobs the worker will
        # cycle through them in <2s. We only care about leak count,
        # not job outcomes.
        assert _wait_until(lambda: q.stats().failed >= 1, timeout=5.0)
        live = _handler_thread_count(prefix)
        # With concurrency=1, one executor handler thread is expected.
        # Pre-fix this was 1-per-attempt-per-job, easily 5+.
        assert live <= 1, f"handler threads leaked under hang: {live} alive"
    finally:
        # Release the hung handlers so the executor's worker thread
        # can exit cleanly when the pool tears down.
        release.set()
        pool.stop(timeout=2)


def test_stop_emits_thread_leaked_audit_when_worker_blocked(
    tmp_path: Path, monkeypatch,
) -> None:
    """#1370: ``pool.stop()`` emits ``worker.thread_leaked`` on join timeout.

    Without this hook the only signal of a leaked worker thread was a
    log line — and the heartbeat / pm doctor surfaces don't ingest
    arbitrary log text. Emitting an audit event lets the fleet count
    these without grepping ``errors.log``.
    """
    q = _make_queue(tmp_path)
    release = threading.Event()

    def hang(payload: dict) -> None:
        release.wait(timeout=30)

    registry = {"h": HandlerSpec("h", hang, timeout_seconds=30, max_attempts=1)}
    pool = JobWorkerPool(q, registry=registry, poll_interval=0.01)

    captured: list[dict] = []

    def fake_emit(**kwargs):
        captured.append(kwargs)

    # Patch the lazy import target.
    import pollypm.audit.log as audit_log
    monkeypatch.setattr(audit_log, "emit", fake_emit)

    pool.start(concurrency=1)
    try:
        q.enqueue("h", max_attempts=1)
        # Wait for the worker to be inside the handler.
        time.sleep(0.2)
        # Fast stop — worker is wedged inside future.result(timeout=30s)
        # so the join will time out.
        pool.stop(timeout=0.2)
    finally:
        release.set()
        # Defensive — give the wedged worker a chance to drop its lock.
        time.sleep(0.05)

    leaked_events = [e for e in captured if e.get("event") == "worker.thread_leaked"]
    assert leaked_events, (
        f"expected at least one worker.thread_leaked audit event, got {captured!r}"
    )
    assert leaked_events[0]["status"] == "warn"
    assert "timeout_seconds" in leaked_events[0]["metadata"]


def test_stop_does_not_emit_thread_leaked_for_clean_shutdown(
    tmp_path: Path, monkeypatch,
) -> None:
    """#1370 happy-path: idle pool shuts down without emitting leak events."""
    q = _make_queue(tmp_path)
    pool = JobWorkerPool(q, registry={}, poll_interval=0.01)

    captured: list[dict] = []

    def fake_emit(**kwargs):
        captured.append(kwargs)

    import pollypm.audit.log as audit_log
    monkeypatch.setattr(audit_log, "emit", fake_emit)

    pool.start(concurrency=2)
    pool.stop(timeout=2)

    leaked_events = [e for e in captured if e.get("event") == "worker.thread_leaked"]
    assert not leaked_events, (
        f"clean shutdown should not emit leak events, got {leaked_events!r}"
    )
