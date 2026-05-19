"""Unit tests for the Postgres-backed durable job queue (#1737 Slice K-jobs)."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

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


def test_enqueue_and_claim(pg_job_queue: JobQueue) -> None:
    q = pg_job_queue
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


def test_complete_marks_done(pg_job_queue: JobQueue) -> None:
    q = pg_job_queue
    jid = q.enqueue("h")
    (job,) = q.claim("w")
    q.complete(job.id)

    stored = q.get(jid)
    assert stored is not None
    assert stored.status is JobStatus.DONE


def test_fail_with_retry_returns_to_queued_with_backoff(pg_schema_pool) -> None:
    # Fixed-delay policy so we can assert exact run_after.
    from pollypm.storage.pg_migrations import apply_migrations

    apply_migrations(pg_schema_pool)
    q = JobQueue(
        pool=pg_schema_pool,
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


def test_fail_exhausted_attempts_moves_to_failed(pg_schema_pool) -> None:
    from pollypm.storage.pg_migrations import apply_migrations

    apply_migrations(pg_schema_pool)
    q = JobQueue(
        pool=pg_schema_pool,
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


def test_fail_no_retry_goes_to_failed_immediately(pg_job_queue: JobQueue) -> None:
    q = pg_job_queue
    jid = q.enqueue("h")
    (job,) = q.claim("w")
    q.fail(job.id, "nope", retry=False)

    stored = q.get(jid)
    assert stored is not None
    assert stored.status is JobStatus.FAILED


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------


def test_dedupe_key_returns_same_id_for_duplicate(pg_job_queue: JobQueue) -> None:
    q = pg_job_queue
    jid1 = q.enqueue("sweep", {"p": "a"}, dedupe_key="sweep:a")
    jid2 = q.enqueue("sweep", {"p": "a"}, dedupe_key="sweep:a")
    jid3 = q.enqueue("sweep", {"p": "a"}, dedupe_key="sweep:a")
    assert jid1 == jid2 == jid3

    stats = q.stats()
    assert stats.queued == 1


def test_dedupe_key_allows_reenqueue_after_completion(pg_job_queue: JobQueue) -> None:
    q = pg_job_queue
    jid1 = q.enqueue("sweep", dedupe_key="sweep:a")
    (job,) = q.claim("w")
    q.complete(job.id)

    # Completed — dedupe key is freed.
    jid2 = q.enqueue("sweep", dedupe_key="sweep:a")
    assert jid2 != jid1


def test_dedupe_key_allows_reenqueue_after_failed(pg_job_queue: JobQueue) -> None:
    q = pg_job_queue
    jid1 = q.enqueue("sweep", dedupe_key="sweep:a")
    (job,) = q.claim("w")
    q.fail(job.id, "err", retry=False)

    jid2 = q.enqueue("sweep", dedupe_key="sweep:a")
    assert jid2 != jid1


def test_has_recent_or_active_dedupe_tracks_active_and_recent_rows(
    pg_job_queue: JobQueue,
) -> None:
    q = pg_job_queue
    before_enqueue = datetime.now(UTC) - timedelta(seconds=1)
    jid = q.enqueue("sweep", dedupe_key="sweep:a")

    assert q.has_recent_or_active_dedupe(
        "sweep:a",
        since=datetime.now(UTC) + timedelta(days=1),
    )

    (job,) = q.claim("w")
    q.complete(job.id)

    assert q.has_recent_or_active_dedupe("sweep:a", since=before_enqueue)
    assert not q.has_recent_or_active_dedupe(
        "sweep:a",
        since=datetime.now(UTC) + timedelta(days=1),
    )
    assert q.get(jid) is not None


def test_late_retry_fail_does_not_resurrect_terminal_dedupe_row(
    pg_job_queue: JobQueue,
) -> None:
    q = pg_job_queue
    jid1 = q.enqueue("sweep", dedupe_key="sweep:a")
    (job,) = q.claim("w")
    q.fail(job.id, "terminal", retry=False)
    jid2 = q.enqueue("sweep", dedupe_key="sweep:a")

    q.fail(job.id, "late timeout", retry=True)

    old = q.get(jid1)
    new = q.get(jid2)
    assert old is not None
    assert old.status is JobStatus.FAILED
    assert q.get_last_error(jid1) == "terminal"
    assert new is not None
    assert new.status is JobStatus.QUEUED


def test_retry_fail_converts_dedupe_violation_to_failed_warning(
    pg_job_queue: JobQueue,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry UPDATE raising ``UniqueViolation`` lands in terminal-failed.

    Production hits this when ``queue.fail(retry=True)`` lands on a
    row whose dedupe slot has already been re-acquired by a peer.
    The partial unique index ``idx_work_jobs_dedupe_queued`` rejects
    the requeue UPDATE; :meth:`JobQueue.fail` catches the violation,
    rolls the transaction back, and marks the row terminal-failed
    with a warning log line.

    Reproducing the natural race against the index requires two
    overlapping rows on the same dedupe_key, which the index itself
    forbids. We synthesise the failure by patching
    :func:`pollypm.jobs.queue._is_unique_violation` to fire on the
    first requeue attempt — same shape as the sqlite test's
    connection-wrapper approach (#1052 regression cover).
    """
    from pollypm.jobs import queue as queue_module

    q = pg_job_queue
    jid = q.enqueue("sweep", dedupe_key="sweep:a")
    (job,) = q.claim("w")
    assert job.id == jid

    # Patch the predicate so the FIRST exception in fail()'s requeue
    # UPDATE is treated as a unique violation. We trigger that
    # exception by patching the retry policy to a tiny window so the
    # row is requeued (which itself won't violate the index — the row
    # is the only one with this dedupe_key — but the predicate-patch
    # makes the regular path look like a collision regardless).
    fired = {"n": 0}
    real_predicate = queue_module._is_unique_violation

    # Simulate a UniqueViolation by raising one ourselves from a
    # patched cursor.execute the first time it sees the requeue UPDATE.
    from psycopg import errors as pg_errors

    class _OneShotViolatingCursor:
        def __init__(self, real_cursor):
            self._real = real_cursor

        def execute(self, sql, *args, **kwargs):
            if (
                not fired["n"]
                and "status = 'queued'" in str(sql)
                and "run_after" in str(sql)
            ):
                fired["n"] += 1
                raise pg_errors.UniqueViolation(
                    "duplicate key value violates unique constraint "
                    "\"idx_work_jobs_dedupe_queued\""
                )
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

        def __enter__(self):
            self._real.__enter__()
            return self

        def __exit__(self, *args):
            return self._real.__exit__(*args)

    # Wrap the pool's connection.cursor so the first matching UPDATE
    # raises UniqueViolation. Roll-and-restore is handled by fail().
    real_connection = q._pool.connection

    class _WrappedConn:
        def __init__(self, ctx):
            self._ctx = ctx

        def __enter__(self):
            self._conn = self._ctx.__enter__()
            real_cursor = self._conn.cursor

            def patched_cursor(*a, **kw):
                return _OneShotViolatingCursor(real_cursor(*a, **kw))

            self._conn.cursor = patched_cursor  # type: ignore[method-assign]
            return self._conn

        def __exit__(self, *args):
            return self._ctx.__exit__(*args)

    monkeypatch.setattr(
        q._pool, "connection", lambda: _WrappedConn(real_connection()),
    )

    caplog.set_level("WARNING", logger="pollypm.jobs.queue")
    q.fail(jid, "late timeout", retry=True)

    assert fired["n"] == 1
    stored = q.get(jid)
    assert stored is not None
    assert stored.status is JobStatus.FAILED
    assert q.get_last_error(jid) == "late timeout"
    assert "dedupe_key collision; marking failed instead" in caplog.text

    # Restore (defensive — monkeypatch will undo at teardown).
    assert real_predicate is queue_module._is_unique_violation


def test_recover_orphaned_claims_resets_status_and_frees_dedupe(
    pg_job_queue: JobQueue,
) -> None:
    """#1071 — orphaned claimed rows must be requeued so dedupe slot frees."""
    q = pg_job_queue
    jid1 = q.enqueue("sweep", dedupe_key="sweep:a")
    (job,) = q.claim("crashed-worker")
    assert job.attempt == 1

    # Pre-recovery: dedupe slot is held by the claimed orphan, so a
    # fresh enqueue short-circuits to the orphan's id.
    jid2 = q.enqueue("sweep", dedupe_key="sweep:a")
    assert jid2 == jid1

    recovered, pruned = q.recover_orphaned_claims()
    assert recovered == 1
    assert pruned == 0

    stored = q.get(jid1)
    assert stored is not None
    assert stored.status is JobStatus.QUEUED
    assert stored.claimed_at is None
    assert stored.claimed_by is None
    assert stored.attempt == 0

    claimed = q.claim("worker-2")
    assert len(claimed) == 1
    assert claimed[0].id == jid1


def test_recover_orphaned_claims_returns_zero_when_none_claimed(
    pg_job_queue: JobQueue,
) -> None:
    q = pg_job_queue
    q.enqueue("sweep")
    assert q.recover_orphaned_claims() == (0, 0)


def test_recover_orphaned_claims_collapses_legacy_duplicates(
    pg_job_queue: JobQueue,
) -> None:
    """#1071 — pre-#1052 cadence ticks accumulated duplicate rows in claimed."""
    q = pg_job_queue
    a = q.enqueue("session.health_sweep")
    q.claim("crashed-1")
    b = q.enqueue("session.health_sweep")
    q.claim("crashed-1")
    c = q.enqueue("session.health_sweep")
    q.claim("crashed-1")
    other = q.enqueue("pane.classify")
    q.claim("crashed-1")

    recovered, pruned = q.recover_orphaned_claims()
    assert recovered == 4  # all four rows requeued
    assert pruned == 2  # two duplicate session.health_sweep rows dropped

    assert q.get(a) is None
    assert q.get(b) is None
    assert q.get(c) is not None
    assert q.get(c).status is JobStatus.QUEUED
    assert q.get(other) is not None
    assert q.get(other).status is JobStatus.QUEUED


def test_recover_orphaned_claims_preserves_queued_rows_when_nothing_claimed(
    pg_job_queue: JobQueue,
) -> None:
    """#1822 — a boot with NO orphaned claims must not prune queued rows.

    Two queued jobs share a handler. ``recover_orphaned_claims`` runs
    when no row is in the ``claimed`` state. The old pg port deleted
    the older queued row unconditionally; the fixed implementation
    leaves both untouched because no recovery happened.
    """
    q = pg_job_queue
    a = q.enqueue("sweep", {"p": "a"})
    b = q.enqueue("sweep", {"p": "b"})

    recovered, pruned = q.recover_orphaned_claims()
    assert recovered == 0
    assert pruned == 0

    # Both rows must still exist and be queued.
    job_a = q.get(a)
    job_b = q.get(b)
    assert job_a is not None and job_a.status is JobStatus.QUEUED
    assert job_b is not None and job_b.status is JobStatus.QUEUED


def test_recover_orphaned_claims_preserves_distinct_dedupe_keys(
    pg_job_queue: JobQueue,
) -> None:
    """#1822 — distinct-dedupe-key rows survive recovery even when claimed.

    Three rows for handler ``h`` with distinct dedupe_keys all wind up
    in the ``claimed`` state (a crashed worker that claimed three rows
    in one boot). Recovery requeues all three; the prune step must
    NOT collapse them because they carry distinct dedupe keys — the
    fix scopes the prune to ``dedupe_key IS NULL`` only.
    """
    q = pg_job_queue
    a = q.enqueue("h", {"p": "a"}, dedupe_key="h:a")
    b = q.enqueue("h", {"p": "b"}, dedupe_key="h:b")
    c = q.enqueue("h", {"p": "c"}, dedupe_key="h:c")
    # Crashed worker claimed all three rows before dying.
    q.claim("crashed-worker", limit=3)

    recovered, pruned = q.recover_orphaned_claims()
    assert recovered == 3
    # All three have distinct (non-NULL) dedupe_keys, so the prune
    # leaves them alone.
    assert pruned == 0

    assert q.get(a) is not None and q.get(a).status is JobStatus.QUEUED
    assert q.get(b) is not None and q.get(b).status is JobStatus.QUEUED
    assert q.get(c) is not None and q.get(c).status is JobStatus.QUEUED


def test_recover_orphaned_claims_preserves_distinct_payloads_same_handler(
    pg_job_queue: JobQueue,
) -> None:
    """#1822 — two queued rows with the same handler + different payloads.

    Both rows have ``dedupe_key=None`` (the legacy shape), but no
    ``claimed`` row exists — there's nothing to recover, so the prune
    step must NOT collapse them. The pre-fix code dropped the older
    one unconditionally.
    """
    q = pg_job_queue
    a = q.enqueue("user.handler", {"p": "first"})
    b = q.enqueue("user.handler", {"p": "second"})

    recovered, pruned = q.recover_orphaned_claims()
    assert recovered == 0
    assert pruned == 0
    assert q.get(a) is not None and q.get(a).status is JobStatus.QUEUED
    assert q.get(b) is not None and q.get(b).status is JobStatus.QUEUED


def test_recover_orphaned_claims_preserves_recovered_sibling_distinct_payload(
    pg_job_queue: JobQueue,
) -> None:
    """#1843 — recovered claim must survive when a sibling queued row has a different payload.

    Scenario:
      1. Enqueue ``user.handler`` with ``{"p": "orphan"}`` and claim
         it as a crashed worker (dedupe_key=None).
      2. Enqueue another ``user.handler`` with ``{"p": "live"}``
         (also dedupe_key=None).
      3. Run ``recover_orphaned_claims``.

    The recovered orphan must survive the prune because its payload
    differs from the live row. The pre-fix prune grouped by
    ``handler_name`` alone, so it kept ``MAX(id)`` (the live row) and
    deleted the recovered orphan — silently dropping durable work.
    """
    q = pg_job_queue
    orphan = q.enqueue("user.handler", {"p": "orphan"})
    (claimed_job,) = q.claim("crashed-worker")
    assert claimed_job.id == orphan

    live = q.enqueue("user.handler", {"p": "live"})

    recovered, pruned = q.recover_orphaned_claims()
    assert recovered == 1, "the orphan must be requeued"
    assert pruned == 0, (
        "distinct-payload rows must NOT be collapsed — the prune step "
        "is for the legacy cadence backlog where every row has an "
        "identical (typically empty) payload"
    )

    orphan_row = q.get(orphan)
    live_row = q.get(live)
    assert orphan_row is not None, (
        "the recovered orphan disappeared — the prune step deleted "
        "durable work it had just requeued (#1843)."
    )
    assert orphan_row.status is JobStatus.QUEUED
    assert live_row is not None and live_row.status is JobStatus.QUEUED


def test_null_dedupe_key_does_not_deduplicate(pg_job_queue: JobQueue) -> None:
    q = pg_job_queue
    jid1 = q.enqueue("sweep", {"p": "a"})
    jid2 = q.enqueue("sweep", {"p": "a"})
    assert jid1 != jid2
    assert q.stats().queued == 2


# ---------------------------------------------------------------------------
# Delayed visibility
# ---------------------------------------------------------------------------


def test_run_after_hides_job_until_due(pg_job_queue: JobQueue) -> None:
    q = pg_job_queue
    future = datetime.now(UTC) + timedelta(hours=1)
    q.enqueue("later", run_after=future)

    assert q.claim("w") == []

    q.enqueue("now")
    claimed = q.claim("w", limit=10)
    assert len(claimed) == 1
    assert claimed[0].handler_name == "now"


def test_run_after_in_the_past_is_immediately_visible(pg_job_queue: JobQueue) -> None:
    q = pg_job_queue
    past = datetime.now(UTC) - timedelta(seconds=60)
    q.enqueue("old", run_after=past)
    claimed = q.claim("w")
    assert len(claimed) == 1


# ---------------------------------------------------------------------------
# Concurrent claim
# ---------------------------------------------------------------------------


def test_concurrent_claim_never_duplicates(pg_job_queue: JobQueue) -> None:
    """``SELECT ... FOR UPDATE SKIP LOCKED`` guarantees no double-claim."""
    q = pg_job_queue
    n_jobs = 100
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

    threads = [threading.Thread(target=worker, args=(f"w-{i}",)) for i in range(4)]
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


def test_stats_reflects_job_states(pg_job_queue: JobQueue) -> None:
    q = pg_job_queue
    for _ in range(3):
        q.enqueue("h")
    q.enqueue("will_fail")
    q.enqueue("will_done")

    (job,) = q.claim("w", limit=1)
    q.complete(job.id)

    stats = q.stats()
    assert stats.queued == 4
    assert stats.done == 1
    assert stats.claimed == 0
    assert stats.failed == 0


def test_claim_limit_bounds_batch_size(pg_job_queue: JobQueue) -> None:
    q = pg_job_queue
    for _ in range(5):
        q.enqueue("h")

    batch = q.claim("w", limit=2)
    assert len(batch) == 2


def test_claim_returns_empty_when_no_jobs(pg_job_queue: JobQueue) -> None:
    q = pg_job_queue
    assert q.claim("w") == []


def test_list_jobs_filter_by_status(pg_job_queue: JobQueue) -> None:
    q = pg_job_queue
    q.enqueue("h1")
    q.enqueue("h2")
    (job,) = q.claim("w", limit=1)
    q.complete(job.id)

    done = q.list_jobs(status=JobStatus.DONE)
    queued = q.list_jobs(status=JobStatus.QUEUED)
    assert len(done) == 1
    assert len(queued) == 1


# ---------------------------------------------------------------------------
# Retry policy (pure helper — no DB needed)
# ---------------------------------------------------------------------------


def test_exponential_backoff_grows() -> None:
    policy = exponential_backoff(base_seconds=1.0, factor=2.0, max_seconds=60.0, jitter=0)
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


def test_enqueue_requires_handler_name(pg_job_queue: JobQueue) -> None:
    q = pg_job_queue
    with pytest.raises(ValueError):
        q.enqueue("")


def test_complete_of_missing_job_is_noop(pg_job_queue: JobQueue) -> None:
    q = pg_job_queue
    q.complete(99999)  # should not raise
    q.fail(99999, "missing", retry=True)  # should not raise


def test_queue_round_trip_via_pool(pg_schema_pool) -> None:
    """A queue built against a pool can be re-opened and read previous rows."""
    from pollypm.storage.pg_migrations import apply_migrations

    apply_migrations(pg_schema_pool)
    q1 = JobQueue(pool=pg_schema_pool)
    jid = q1.enqueue("h", {"a": 1})
    q1.close()

    q2 = JobQueue(pool=pg_schema_pool)
    job = q2.get(jid)
    assert job is not None
    assert job.handler_name == "h"
    assert job.payload == {"a": 1}
