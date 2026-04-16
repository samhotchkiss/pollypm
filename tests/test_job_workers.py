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
