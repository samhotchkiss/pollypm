"""Unit tests for the SQLite-backed durable job queue."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pollypm.jobs import (
    Job,
    JobQueue,
    JobStatus,
    exponential_backoff,
)


# ---------------------------------------------------------------------------
# Basic lifecycle
# ---------------------------------------------------------------------------


def test_enqueue_and_claim(tmp_path: Path) -> None:
    q = JobQueue(db_path=tmp_path / "q.db")
    jid = q.enqueue("hello", {"name": "world"})
    assert jid > 0

    claimed = q.claim("worker-1")
    assert len(claimed) == 1
    job = claimed[0]
    assert isinstance(job, Job)
    assert job.id == jid
    assert job.handler_name == "hello"
    assert job.payload == {"name": "world"}
    assert job.attempt == 1
    assert job.status is JobStatus.CLAIMED
    assert job.claimed_by == "worker-1"


def test_complete_marks_done(tmp_path: Path) -> None:
    q = JobQueue(db_path=tmp_path / "q.db")
    jid = q.enqueue("h")
    (job,) = q.claim("w")
    q.complete(job.id)

    stored = q.get(jid)
    assert stored is not None
    assert stored.status is JobStatus.DONE


def test_fail_with_retry_returns_to_queued_with_backoff(tmp_path: Path) -> None:
    # Use a fixed-delay policy so we can assert exact run_after.
    q = JobQueue(
        db_path=tmp_path / "q.db",
        retry_policy=lambda attempt: timedelta(seconds=5),
    )
    q.enqueue("h")
    (job,) = q.claim("w")
    before = datetime.now(UTC)
    q.fail(job.id, "boom", retry=True)

    stored = q.get(job.id)
    assert stored is not None
    assert stored.status is JobStatus.QUEUED
    assert stored.run_after >= before + timedelta(seconds=4)
    assert stored.claimed_by is None
    assert q.get_last_error(job.id) == "boom"


def test_fail_exhausted_attempts_moves_to_failed(tmp_path: Path) -> None:
    q = JobQueue(
        db_path=tmp_path / "q.db",
        retry_policy=lambda attempt: timedelta(seconds=0),
    )
    jid = q.enqueue("h", max_attempts=2)

    # Attempt 1: claim + fail → goes back to queued.
    (job,) = q.claim("w")
    assert job.attempt == 1
    q.fail(job.id, "first", retry=True)

    stored = q.get(jid)
    assert stored is not None
    assert stored.status is JobStatus.QUEUED

    # Attempt 2: claim + fail → goes to failed (max_attempts=2).
    (job,) = q.claim("w")
    assert job.attempt == 2
    q.fail(job.id, "second", retry=True)

    final = q.get(jid)
    assert final is not None
    assert final.status is JobStatus.FAILED
    assert q.get_last_error(jid) == "second"


def test_fail_no_retry_goes_to_failed_immediately(tmp_path: Path) -> None:
    q = JobQueue(db_path=tmp_path / "q.db")
    jid = q.enqueue("h")
    (job,) = q.claim("w")
    q.fail(job.id, "nope", retry=False)

    stored = q.get(jid)
    assert stored is not None
    assert stored.status is JobStatus.FAILED


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------


def test_dedupe_key_returns_same_id_for_duplicate(tmp_path: Path) -> None:
    q = JobQueue(db_path=tmp_path / "q.db")
    jid1 = q.enqueue("sweep", {"p": "a"}, dedupe_key="sweep:a")
    jid2 = q.enqueue("sweep", {"p": "a"}, dedupe_key="sweep:a")
    jid3 = q.enqueue("sweep", {"p": "a"}, dedupe_key="sweep:a")
    assert jid1 == jid2 == jid3

    stats = q.stats()
    assert stats.queued == 1


def test_dedupe_key_allows_reenqueue_after_completion(tmp_path: Path) -> None:
    q = JobQueue(db_path=tmp_path / "q.db")
    jid1 = q.enqueue("sweep", dedupe_key="sweep:a")
    (job,) = q.claim("w")
    q.complete(job.id)

    # Completed — dedupe key is freed.
    jid2 = q.enqueue("sweep", dedupe_key="sweep:a")
    assert jid2 != jid1


def test_dedupe_key_allows_reenqueue_after_failed(tmp_path: Path) -> None:
    q = JobQueue(db_path=tmp_path / "q.db")
    jid1 = q.enqueue("sweep", dedupe_key="sweep:a")
    (job,) = q.claim("w")
    q.fail(job.id, "err", retry=False)

    jid2 = q.enqueue("sweep", dedupe_key="sweep:a")
    assert jid2 != jid1


def test_null_dedupe_key_does_not_deduplicate(tmp_path: Path) -> None:
    q = JobQueue(db_path=tmp_path / "q.db")
    jid1 = q.enqueue("sweep", {"p": "a"})
    jid2 = q.enqueue("sweep", {"p": "a"})
    assert jid1 != jid2
    assert q.stats().queued == 2


# ---------------------------------------------------------------------------
# Delayed visibility
# ---------------------------------------------------------------------------


def test_run_after_hides_job_until_due(tmp_path: Path) -> None:
    q = JobQueue(db_path=tmp_path / "q.db")
    future = datetime.now(UTC) + timedelta(hours=1)
    jid = q.enqueue("later", run_after=future)

    # Not yet visible.
    assert q.claim("w") == []

    # Reschedule to the past by manipulating via a fail-then-update? simpler:
    # insert a second job with run_after in the past and verify ordering.
    q.enqueue("now")
    claimed = q.claim("w", limit=10)
    assert len(claimed) == 1
    assert claimed[0].handler_name == "now"


def test_run_after_in_the_past_is_immediately_visible(tmp_path: Path) -> None:
    q = JobQueue(db_path=tmp_path / "q.db")
    past = datetime.now(UTC) - timedelta(seconds=60)
    q.enqueue("old", run_after=past)
    claimed = q.claim("w")
    assert len(claimed) == 1


# ---------------------------------------------------------------------------
# Concurrent claim
# ---------------------------------------------------------------------------


def test_concurrent_claim_never_duplicates(tmp_path: Path) -> None:
    q = JobQueue(db_path=tmp_path / "q.db")
    n_jobs = 200
    for i in range(n_jobs):
        q.enqueue("h", {"i": i})

    claimed_ids: list[int] = []
    claimed_lock = threading.Lock()

    def worker(worker_id: str) -> None:
        while True:
            batch = q.claim(worker_id, limit=5)
            if not batch:
                return
            with claimed_lock:
                claimed_ids.extend(job.id for job in batch)
            for job in batch:
                q.complete(job.id)

    threads = [threading.Thread(target=worker, args=(f"w-{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(claimed_ids) == n_jobs
    assert len(set(claimed_ids)) == n_jobs  # no duplicates
    stats = q.stats()
    assert stats.done == n_jobs
    assert stats.queued == 0
    assert stats.claimed == 0


# ---------------------------------------------------------------------------
# Stats / list
# ---------------------------------------------------------------------------


def test_stats_reflects_job_states(tmp_path: Path) -> None:
    q = JobQueue(db_path=tmp_path / "q.db")
    for _ in range(3):
        q.enqueue("h")
    q.enqueue("will_fail")
    q.enqueue("will_done")

    # One claimed+done.
    (job,) = q.claim("w", limit=1)  # can't rely on ordering beyond run_after
    q.complete(job.id)

    stats = q.stats()
    assert stats.queued == 4
    assert stats.done == 1
    assert stats.claimed == 0
    assert stats.failed == 0


def test_claim_limit_bounds_batch_size(tmp_path: Path) -> None:
    q = JobQueue(db_path=tmp_path / "q.db")
    for _ in range(5):
        q.enqueue("h")

    batch = q.claim("w", limit=2)
    assert len(batch) == 2


def test_claim_returns_empty_when_no_jobs(tmp_path: Path) -> None:
    q = JobQueue(db_path=tmp_path / "q.db")
    assert q.claim("w") == []


def test_list_jobs_filter_by_status(tmp_path: Path) -> None:
    q = JobQueue(db_path=tmp_path / "q.db")
    q.enqueue("h1")
    q.enqueue("h2")
    (job,) = q.claim("w", limit=1)
    q.complete(job.id)

    done = q.list_jobs(status=JobStatus.DONE)
    queued = q.list_jobs(status=JobStatus.QUEUED)
    assert len(done) == 1
    assert len(queued) == 1


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


def test_exponential_backoff_grows() -> None:
    policy = exponential_backoff(base_seconds=1.0, factor=2.0, max_seconds=60.0, jitter=0)
    # Deterministic with no jitter.
    assert policy(1) == timedelta(seconds=1)
    assert policy(2) == timedelta(seconds=2)
    assert policy(3) == timedelta(seconds=4)
    assert policy(4) == timedelta(seconds=8)
    assert policy(10) == timedelta(seconds=60)  # capped


def test_exponential_backoff_respects_max() -> None:
    policy = exponential_backoff(base_seconds=1.0, factor=2.0, max_seconds=5.0, jitter=0)
    assert policy(100) == timedelta(seconds=5)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_enqueue_requires_handler_name(tmp_path: Path) -> None:
    q = JobQueue(db_path=tmp_path / "q.db")
    with pytest.raises(ValueError):
        q.enqueue("")


def test_complete_of_missing_job_is_noop(tmp_path: Path) -> None:
    q = JobQueue(db_path=tmp_path / "q.db")
    q.complete(99999)  # should not raise
    q.fail(99999, "missing", retry=True)  # should not raise


def test_queue_can_reopen_existing_db(tmp_path: Path) -> None:
    path = tmp_path / "q.db"
    q1 = JobQueue(db_path=path)
    jid = q1.enqueue("h", {"a": 1})
    q1.close()

    q2 = JobQueue(db_path=path)
    job = q2.get(jid)
    assert job is not None
    assert job.handler_name == "h"
    assert job.payload == {"a": 1}
