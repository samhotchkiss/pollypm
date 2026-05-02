"""Tests for the ``stuck_claims.sweep`` recurring handler (#1049).

The sweep periodically scans the ``work_jobs`` table for rows stuck in
``claimed`` past their handler's timeout window and force-fails them.
This recovers jobs orphaned when the in-band watchdog
(``workers.py:_run_one``) hits its own retry-budget exhaustion under
sustained WAL contention.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from pollypm.jobs import JobQueue, JobStatus
from pollypm.plugins_builtin.core_recurring.maintenance import (
    stuck_claims_sweep_handler,
)


def _force_claim(q: JobQueue, job_id: int, *, claimed_at: datetime) -> None:
    """Stamp a claim with a custom ``claimed_at`` (test seam).

    ``JobQueue.claim`` always uses ``now`` for ``claimed_at``; tests need
    to backdate so the cutoff path is exercised without sleeping.
    """
    iso = claimed_at.astimezone(UTC).isoformat()
    with q._lock:  # noqa: SLF001 — test-only seam
        q._conn.execute(  # noqa: SLF001
            """
            UPDATE work_jobs
            SET status = 'claimed', claimed_at = ?, claimed_by = 'worker-test',
                attempt = 1
            WHERE id = ?
            """,
            (iso, int(job_id)),
        )


def test_stuck_claim_past_cutoff_is_force_failed_with_retry(tmp_path: Path) -> None:
    """A claim stamped 30 minutes ago for a 120 s handler is recovered."""
    q = JobQueue(db_path=tmp_path / "q.db")
    jid = q.enqueue("session.health_sweep", max_attempts=3)
    backdated = datetime.now(UTC) - timedelta(minutes=30)
    _force_claim(q, jid, claimed_at=backdated)

    summary = stuck_claims_sweep_handler(
        {},
        queue=q,
        # session.health_sweep timeout is 120 s in production registration,
        # so cutoff = max(240s, 600s) = 600s. Backdated 30 min — well past.
        handler_timeouts={"session.health_sweep": 120.0},
    )

    assert summary["scanned"] == 1
    assert summary["recovered"] == 1
    assert summary["skipped_recent"] == 0
    assert summary["errors"] == 0

    stored = q.get(jid)
    assert stored is not None
    # retry=True path: attempt < max_attempts so it goes back to queued.
    assert stored.status is JobStatus.QUEUED
    assert stored.claimed_at is None
    assert stored.claimed_by is None
    assert q.get_last_error(jid) == "auto-recovered stuck claim"


def test_recent_claim_within_cutoff_is_left_alone(tmp_path: Path) -> None:
    """A claim 30 s old for a 120 s handler stays claimed (cutoff floor 600 s)."""
    q = JobQueue(db_path=tmp_path / "q.db")
    jid = q.enqueue("session.health_sweep", max_attempts=3)
    fresh = datetime.now(UTC) - timedelta(seconds=30)
    _force_claim(q, jid, claimed_at=fresh)

    summary = stuck_claims_sweep_handler(
        {},
        queue=q,
        handler_timeouts={"session.health_sweep": 120.0},
    )

    assert summary["scanned"] == 1
    assert summary["recovered"] == 0
    assert summary["skipped_recent"] == 1

    stored = q.get(jid)
    assert stored is not None
    assert stored.status is JobStatus.CLAIMED  # untouched
    assert stored.claimed_at is not None


def test_sweep_is_idempotent(tmp_path: Path) -> None:
    """Running twice in a row produces the same final state."""
    q = JobQueue(db_path=tmp_path / "q.db")
    jid = q.enqueue("session.health_sweep", max_attempts=3)
    backdated = datetime.now(UTC) - timedelta(minutes=30)
    _force_claim(q, jid, claimed_at=backdated)

    timeouts = {"session.health_sweep": 120.0}

    first = stuck_claims_sweep_handler({}, queue=q, handler_timeouts=timeouts)
    state_after_first = q.get(jid)
    assert state_after_first is not None
    assert state_after_first.status is JobStatus.QUEUED
    assert first["recovered"] == 1

    # Second pass: the row is back in 'queued', so it's no longer in the
    # find_stuck_claims result set. Counters should be all zero.
    second = stuck_claims_sweep_handler({}, queue=q, handler_timeouts=timeouts)
    assert second["scanned"] == 0
    assert second["recovered"] == 0
    assert second["skipped_recent"] == 0
    assert second["errors"] == 0

    # And the underlying row is still in the queued state we expect — not
    # double-failed, not pushed to terminal failed.
    stored = q.get(jid)
    assert stored is not None
    assert stored.status is JobStatus.QUEUED


def test_unknown_handler_uses_600s_floor(tmp_path: Path) -> None:
    """A claim for a handler with no registered timeout still recovers past 600 s."""
    q = JobQueue(db_path=tmp_path / "q.db")
    jid = q.enqueue("ghost.handler.no_longer_registered", max_attempts=3)
    backdated = datetime.now(UTC) - timedelta(minutes=20)
    _force_claim(q, jid, claimed_at=backdated)

    # Empty timeouts dict: the handler isn't in the registry. The 600 s
    # safety floor still applies, and 20 min > 600 s, so it recovers.
    summary = stuck_claims_sweep_handler({}, queue=q, handler_timeouts={})

    assert summary["recovered"] == 1
    stored = q.get(jid)
    assert stored is not None
    assert stored.status is JobStatus.QUEUED


def test_unknown_handler_within_floor_is_left_alone(tmp_path: Path) -> None:
    """An 8-min-old claim for an unknown handler stays — under the 600 s floor."""
    q = JobQueue(db_path=tmp_path / "q.db")
    jid = q.enqueue("ghost.handler", max_attempts=3)
    backdated = datetime.now(UTC) - timedelta(minutes=8)
    _force_claim(q, jid, claimed_at=backdated)

    summary = stuck_claims_sweep_handler({}, queue=q, handler_timeouts={})

    assert summary["scanned"] == 1
    assert summary["skipped_recent"] == 1
    stored = q.get(jid)
    assert stored is not None
    assert stored.status is JobStatus.CLAIMED


def test_max_attempts_exhausted_goes_to_terminal_failed(tmp_path: Path) -> None:
    """A stuck claim whose attempt == max_attempts moves to terminal failed."""
    q = JobQueue(db_path=tmp_path / "q.db")
    jid = q.enqueue("session.health_sweep", max_attempts=1)
    backdated = datetime.now(UTC) - timedelta(minutes=30)
    _force_claim(q, jid, claimed_at=backdated)

    summary = stuck_claims_sweep_handler(
        {},
        queue=q,
        handler_timeouts={"session.health_sweep": 120.0},
    )

    # attempt=1 from _force_claim, max_attempts=1 → queue.fail with
    # retry=True still ends in terminal 'failed' (the queue itself
    # checks attempt >= max_attempts).
    assert summary["scanned"] == 1
    assert summary["failed_terminal"] == 1
    assert summary["recovered"] == 0

    stored = q.get(jid)
    assert stored is not None
    assert stored.status is JobStatus.FAILED


def test_find_stuck_claims_returns_only_claimed(tmp_path: Path) -> None:
    """``JobQueue.find_stuck_claims`` skips queued/done/failed rows."""
    q = JobQueue(db_path=tmp_path / "q.db")

    # Queued (untouched) and the row we'll force into 'claimed'.
    queued_id = q.enqueue("a")
    claimed_id = q.enqueue("b")
    _force_claim(
        q, claimed_id, claimed_at=datetime.now(UTC) - timedelta(minutes=30),
    )

    # Drain the queued row → done. ``claim`` returns FIFO by run_after,id.
    (queued_job,) = q.claim("worker-x")
    assert queued_job.id == queued_id
    q.complete(queued_job.id)

    # Enqueue + claim + fail-terminal — ends in 'failed', shouldn't show up.
    failed_id = q.enqueue("d", max_attempts=1)
    (failed_job,) = q.claim("worker-x")
    assert failed_job.id == failed_id
    q.fail(failed_id, "boom", retry=False)

    stuck = q.find_stuck_claims()
    assert {j.id for j in stuck} == {claimed_id}
