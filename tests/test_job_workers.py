"""Unit tests for the job worker pool (pg-backed, #1737 Slice K-jobs)."""

from __future__ import annotations

import threading
import time

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


# ---------------------------------------------------------------------------
# Basic success / failure
# ---------------------------------------------------------------------------


def test_successful_handler_marks_job_done(pg_job_queue: JobQueue) -> None:
    q = pg_job_queue
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


def test_handler_exception_fails_with_retry(pg_job_queue: JobQueue) -> None:
    q = pg_job_queue

    def boom(payload: dict) -> None:
        raise RuntimeError("nope")

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


def test_handler_exception_retries_until_exhausted(pg_job_queue: JobQueue) -> None:
    q = pg_job_queue
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


def test_handler_timeout_fails_job(pg_job_queue: JobQueue) -> None:
    q = pg_job_queue

    def slow(payload: dict) -> None:
        time.sleep(5)

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


def test_unknown_handler_fails_permanently(pg_job_queue: JobQueue) -> None:
    q = pg_job_queue

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


def test_failing_handler_does_not_wedge_others(pg_job_queue: JobQueue) -> None:
    q = pg_job_queue
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
# Concurrency
# ---------------------------------------------------------------------------


def test_pool_drains_queue_concurrently(pg_job_queue: JobQueue) -> None:
    q = pg_job_queue

    def sleepy(payload: dict) -> None:
        time.sleep(0.1)

    registry = {"s": HandlerSpec("s", sleepy, timeout_seconds=5)}
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

    assert elapsed < 1.0, f"pool took {elapsed:.3f}s (expected concurrency speedup)"


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


def test_stop_is_idempotent(pg_job_queue: JobQueue) -> None:
    q = pg_job_queue
    pool = JobWorkerPool(q, registry={}, poll_interval=0.01)
    pool.start(concurrency=1)
    pool.stop(timeout=1)
    pool.stop(timeout=1)  # second stop shouldn't error


def test_double_start_raises(pg_job_queue: JobQueue) -> None:
    q = pg_job_queue
    pool = JobWorkerPool(q, registry={}, poll_interval=0.01)
    pool.start(concurrency=1)
    try:
        with pytest.raises(RuntimeError):
            pool.start(concurrency=1)
    finally:
        pool.stop(timeout=1)


def test_concurrency_must_be_positive(pg_job_queue: JobQueue) -> None:
    q = pg_job_queue
    pool = JobWorkerPool(q, registry={}, poll_interval=0.01)
    with pytest.raises(ValueError):
        pool.start(concurrency=0)
    with pytest.raises(ValueError):
        pool.start(concurrency=-1)


def test_stop_waits_for_in_flight_job(pg_job_queue: JobQueue) -> None:
    q = pg_job_queue
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

    def run_stop() -> None:
        pool.stop(timeout=3)

    stop_thread = threading.Thread(target=run_stop)
    stop_thread.start()
    time.sleep(0.1)
    release.set()
    stop_thread.join(timeout=3)
    assert not stop_thread.is_alive()
    assert completed.is_set()


# ---------------------------------------------------------------------------
# Closed-pool recovery (#1006 pg analogue)
# ---------------------------------------------------------------------------


def test_pool_drains_when_pg_pool_closed_under_workers(pg_schema_pool) -> None:
    """A closed pg pool trips the stop event, not a tight-loop traceback.

    The pg analogue of #1006: the production cockpit hits this when a
    sibling process closes the pg pool while workers are still polling
    ``queue.claim``. Pre-fix, every worker thread tight-looped a
    ``PoolClosed`` traceback into ``errors.log`` until the join
    timeout lapsed and rail_daemon was zombied. The fix recognises the
    closed-pool exception via ``_is_pool_closed_error`` and trips the
    stop event so sibling workers exit on their next short-poll.
    """
    from pollypm.storage.pg_migrations import apply_migrations

    apply_migrations(pg_schema_pool)
    q = JobQueue(pool=pg_schema_pool)

    drained = threading.Event()
    completed = [0]
    enqueue_lock = threading.Lock()

    def quick(payload: dict) -> None:
        with enqueue_lock:
            completed[0] += 1
            if completed[0] >= 1:
                drained.set()

    registry = {"h": HandlerSpec("h", quick, timeout_seconds=5)}
    pool = JobWorkerPool(q, registry=registry, poll_interval=0.02)
    pool.start(concurrency=4)
    try:
        for _ in range(2):
            q.enqueue("h")
        assert drained.wait(timeout=3), "no job ever completed"

        # Close the pool. The next claim from every worker raises
        # PoolClosed; ``_handle_closed_db`` trips the stop event so
        # they exit cleanly instead of tight-looping.
        pg_schema_pool.close()

        t0 = time.monotonic()
        pool.stop(timeout=3.0)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.5, (
            f"pool.stop took {elapsed:.2f}s — workers tight-looped on "
            "closed-pool instead of exiting cleanly"
        )
    finally:
        pool.stop(timeout=1.0)

    alive = [
        t for t in threading.enumerate()
        if t.name.startswith("pollypm-jobworker-")
        and "handler" not in t.name
    ]
    assert not alive, f"workers still alive after stop: {[t.name for t in alive]}"


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_metrics_track_per_handler_counts_and_duration(pg_job_queue: JobQueue) -> None:
    q = pg_job_queue

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
    return sum(
        1 for t in threading.enumerate()
        if t.name.startswith(f"{prefix}-handler") and t.is_alive()
    )


def test_handler_thread_does_not_leak_per_attempt(pg_job_queue: JobQueue) -> None:
    """#1370: handler invocations must reuse one thread per worker."""
    q = pg_job_queue

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
        live = _handler_thread_count(prefix)
        assert live <= 2, f"handler threads leaked: {live} alive"
    finally:
        pool.stop(timeout=2)


def test_timed_out_handler_does_not_spawn_new_thread_each_retry(
    pg_job_queue: JobQueue,
) -> None:
    """#1370: a hung handler must not multiply handler threads."""
    q = pg_job_queue
    release = threading.Event()

    def hang(payload: dict) -> None:
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
        assert _wait_until(lambda: q.stats().failed >= 1, timeout=5.0)
        live = _handler_thread_count(prefix)
        assert live <= 1, f"handler threads leaked under hang: {live} alive"
    finally:
        release.set()
        pool.stop(timeout=2)


def test_stop_emits_thread_leaked_audit_when_worker_blocked(
    pg_job_queue: JobQueue, monkeypatch,
) -> None:
    """#1370: ``pool.stop()`` emits ``worker.thread_leaked`` on join timeout."""
    q = pg_job_queue
    release = threading.Event()

    def hang(payload: dict) -> None:
        release.wait(timeout=30)

    registry = {"h": HandlerSpec("h", hang, timeout_seconds=30, max_attempts=1)}
    pool = JobWorkerPool(q, registry=registry, poll_interval=0.01)

    captured: list[dict] = []

    def fake_emit(**kwargs):
        captured.append(kwargs)

    import pollypm.audit.log as audit_log
    monkeypatch.setattr(audit_log, "emit", fake_emit)

    pool.start(concurrency=1)
    try:
        q.enqueue("h", max_attempts=1)
        time.sleep(0.2)
        pool.stop(timeout=0.2)
    finally:
        release.set()
        time.sleep(0.05)

    leaked_events = [e for e in captured if e.get("event") == "worker.thread_leaked"]
    assert leaked_events, (
        f"expected at least one worker.thread_leaked audit event, got {captured!r}"
    )
    assert leaked_events[0]["status"] == "warn"
    assert "timeout_seconds" in leaked_events[0]["metadata"]


def test_stop_does_not_emit_thread_leaked_for_clean_shutdown(
    pg_job_queue: JobQueue, monkeypatch,
) -> None:
    """#1370 happy-path: idle pool shuts down without emitting leak events."""
    q = pg_job_queue
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
