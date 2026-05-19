"""Postgres-backed durable job queue (issue #1737, Slice K-jobs).

The queue lives in the ``work_jobs`` table installed by migration 0001
in :mod:`pollypm.storage.pg_schema`::

    CREATE TABLE work_jobs (
        id            bigserial PRIMARY KEY,
        handler_name  text NOT NULL,
        payload_json  jsonb NOT NULL,
        status        text NOT NULL DEFAULT 'queued',  -- queued|claimed|done|failed
        attempt       int NOT NULL DEFAULT 0,
        max_attempts  int NOT NULL DEFAULT 3,
        dedupe_key    text,
        enqueued_at   timestamptz NOT NULL,
        run_after     timestamptz NOT NULL,
        claimed_at    timestamptz,
        claimed_by    text,
        finished_at   timestamptz,
        last_error    text
    );

    CREATE INDEX idx_work_jobs_claim
        ON work_jobs(status, run_after, id);
    CREATE UNIQUE INDEX idx_work_jobs_dedupe_queued
        ON work_jobs(dedupe_key)
        WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'claimed');

Atomic claim
------------
Claim uses ``SELECT ... FOR UPDATE SKIP LOCKED`` so two workers never
collide on the same row even under heavy contention — pg's row-level
locks make the sqlite "two-step claim under a single writer" pattern
unnecessary. SKIP LOCKED also removes the busy-wait window that
plagued the sqlite queue under heartbeat + worker-pool load (#1018);
contending workers immediately see the next free row instead of
blocking on the locked one.

API parity
----------
This module mirrors the public surface of the historical sqlite queue
(``Job``, ``JobQueue``, ``JobStatus``, ``QueueStats``, ``RetryPolicy``,
``exponential_backoff``) and every method signature. Callers (heartbeat
boot, plugin handlers, the ``pm jobs`` CLI, tests) do not have to
change. The constructor accepts the legacy ``db_path``/``connection``
keyword arguments for back-compat — both are ignored at runtime; the
queue always runs against the process-wide pg pool returned by
:func:`pollypm.storage.pg_pool.get_rw_pool`.
"""

from __future__ import annotations

import logging
import random
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, TypeVar

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

    from pollypm.models import PollyPMConfig


logger = logging.getLogger(__name__)

T = TypeVar("T")


__all__ = [
    "Job",
    "JobId",
    "JobQueue",
    "JobStatus",
    "QueueStats",
    "RetryPolicy",
    "exponential_backoff",
]


JobId = int


class JobStatus(str, Enum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    DONE = "done"
    FAILED = "failed"


@dataclass(slots=True)
class Job:
    """A claimed unit of work. ``payload`` is already decoded from JSON."""

    id: JobId
    handler_name: str
    payload: dict[str, Any]
    attempt: int
    max_attempts: int
    dedupe_key: str | None
    enqueued_at: datetime
    run_after: datetime
    claimed_at: datetime | None
    claimed_by: str | None
    status: JobStatus = JobStatus.CLAIMED


@dataclass(slots=True)
class QueueStats:
    queued: int
    claimed: int
    done: int
    failed: int

    @property
    def total(self) -> int:
        return self.queued + self.claimed + self.done + self.failed


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


RetryPolicy = Callable[[int], timedelta]
"""Given ``attempt`` (the attempt number that just failed), return the delay
before the next attempt."""


def exponential_backoff(
    *,
    base_seconds: float = 2.0,
    factor: float = 2.0,
    max_seconds: float = 300.0,
    jitter: float = 0.1,
) -> RetryPolicy:
    """Exponential backoff with optional jitter.

    Delay for attempt ``n`` is ``min(base * factor**(n-1), max) * (1 +/- jitter)``.
    """

    def policy(attempt: int) -> timedelta:
        n = max(1, int(attempt))
        delay = base_seconds * (factor ** (n - 1))
        delay = min(delay, max_seconds)
        if jitter > 0:
            delay *= 1 + random.uniform(-jitter, jitter)
        return timedelta(seconds=max(0.0, delay))

    return policy


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _ensure_utc(value: datetime | None) -> datetime | None:
    """Coerce a returned timestamp to tz-aware UTC.

    psycopg returns ``timestamptz`` rows as tz-aware datetimes already,
    but tests that bypass the pool (e.g. seed direct rows) sometimes
    drop the tz. This helper keeps the Job dataclass consistent with
    the historical sqlite behaviour (always tz-aware UTC).
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _is_pool_closed_error(exc: BaseException) -> bool:
    """True iff ``exc`` is psycopg-pool's "the pool is shut down" signal.

    This is the pg analogue of the sqlite ``ProgrammingError: Cannot
    operate on a closed database`` (#1006). When the process-wide
    :class:`psycopg_pool.ConnectionPool` is closed under a live worker
    (test teardown, ``pg_pool_shutdown()``, ``pm reset``) any
    ``pool.connection()`` raises ``PoolClosed`` and we cannot recover.
    The worker pool reads this predicate via the ``_is_closed_db_error``
    shim in :mod:`pollypm.jobs.workers` and trips its stop event so
    sibling workers exit cleanly instead of tight-looping a traceback.
    """
    cls_name = exc.__class__.__name__
    if cls_name == "PoolClosed":
        return True
    # ``InterfaceError: connection is closed`` is what psycopg raises
    # when a per-connection close races a queue operation. Treat it the
    # same way — the pool will produce a fresh one on the next call,
    # but the in-flight statement is dead.
    try:
        import psycopg

        if isinstance(exc, psycopg.InterfaceError):
            msg = str(exc).lower()
            if "closed" in msg:
                return True
    except ImportError:  # pragma: no cover - psycopg is a hard runtime dep
        pass
    return False


def _is_unique_violation(exc: BaseException) -> bool:
    """True iff ``exc`` is a pg unique-constraint violation.

    The optimistic-locking pattern lives at two callsites:

    * :meth:`JobQueue.enqueue` racing on the partial unique
      ``idx_work_jobs_dedupe_queued`` — handled by re-reading the
      winner.
    * :meth:`JobQueue.fail` requeueing a failed-but-still-dedupe-keyed
      row when a peer has already enqueued a replacement — handled by
      marking the row terminal-failed (mirrors the sqlite behaviour
      from #1052 / #1071).

    Both surfaces translate the violation into an explicit recovery
    path; the predicate here keeps the recognition logic in one place.
    """
    try:
        from psycopg import errors as pg_errors

        return isinstance(exc, pg_errors.UniqueViolation)
    except ImportError:  # pragma: no cover - psycopg is a hard runtime dep
        return False


class JobQueue:
    """Postgres-backed durable job queue.

    Construction
    ------------
    ``JobQueue()`` is the canonical form — the queue reads its
    connection pool from :func:`pollypm.storage.pg_pool.get_rw_pool`.
    The legacy keyword arguments ``db_path`` and ``connection`` are
    accepted but ignored: every call goes through the pool regardless.
    They remain in the signature so heartbeat / CLI / plugin callers
    don't have to be touched as part of the pg cutover (#1737 Slice K).

    Tests that need a private schema namespace inject a per-test pool
    via the ``pool=`` keyword (see ``tests/conftest_pg.py``'s
    ``pg_schema_pool`` fixture).
    """

    def __init__(
        self,
        *,
        db_path: Path | str | None = None,  # noqa: ARG002 — back-compat shim
        connection: object | None = None,  # noqa: ARG002 — back-compat shim
        pool: "ConnectionPool | None" = None,
        config: "PollyPMConfig | None" = None,
        default_max_attempts: int = 3,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        if pool is None:
            from pollypm.storage.pg_pool import get_rw_pool

            pool = get_rw_pool(config)
        self._pool = pool
        # ``_lock`` is no longer load-bearing — psycopg + pg row-level
        # locks serialise writers without an application lock — but
        # the attribute is retained as an RLock so callers reaching into
        # the queue's internal lock (legacy maintenance handlers) keep
        # working until the K-deletion agent migrates them.
        self._lock = threading.RLock()
        self._closed = False
        self.default_max_attempts = default_max_attempts
        self.retry_policy = retry_policy or exponential_backoff()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """No-op — the pool's lifetime is owned by ``pg_pool``.

        The sqlite queue closed its private connection here; the pg
        queue shares a process-wide pool, so explicit close from a
        single consumer would tear down every other caller. Set the
        ``_closed`` flag so context-manager teardown still observes the
        same shape, but do not touch the pool itself.
        """
        self._closed = True

    def __enter__(self) -> "JobQueue":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------

    def enqueue(
        self,
        handler_name: str,
        payload: dict[str, Any] | None = None,
        *,
        dedupe_key: str | None = None,
        run_after: datetime | None = None,
        max_attempts: int | None = None,
    ) -> JobId:
        """Insert a job. Idempotent when ``dedupe_key`` is set.

        If a queued-or-claimed job with the same ``dedupe_key`` already
        exists, the existing job's id is returned and no new row is
        inserted.
        """
        if not handler_name:
            raise ValueError("handler_name is required")
        from psycopg.types.json import Json

        payload_json = Json(payload or {})
        run_after_ts = (run_after or _now_utc()).astimezone(UTC)
        max_att = max_attempts if max_attempts is not None else self.default_max_attempts
        now = _now_utc()

        with self._pool.connection() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                if dedupe_key is not None:
                    cur.execute(
                        """
                        SELECT id FROM work_jobs
                        WHERE dedupe_key = %s
                          AND status IN ('queued', 'claimed')
                        LIMIT 1
                        """,
                        (dedupe_key,),
                    )
                    existing = cur.fetchone()
                    if existing is not None:
                        conn.rollback()
                        return int(existing[0])

                try:
                    cur.execute(
                        """
                        INSERT INTO work_jobs (
                            handler_name, payload_json, status, attempt,
                            max_attempts, dedupe_key, enqueued_at, run_after
                        ) VALUES (%s, %s, 'queued', 0, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            handler_name,
                            payload_json,
                            max_att,
                            dedupe_key,
                            now,
                            run_after_ts,
                        ),
                    )
                except Exception as exc:  # noqa: BLE001
                    # Raced with another enqueue on the same dedupe_key
                    # — psycopg raises UniqueViolation. Roll back the
                    # aborted transaction and look up the winner.
                    if not _is_unique_violation(exc):
                        raise
                    conn.rollback()
                    if dedupe_key is None:
                        # Two anonymous (no-dedupe) jobs can't collide
                        # on the partial unique index, so any unique
                        # violation without a dedupe_key is unexpected.
                        raise
                    with conn.cursor() as look_cur:
                        look_cur.execute(
                            """
                            SELECT id FROM work_jobs
                            WHERE dedupe_key = %s
                              AND status IN ('queued', 'claimed')
                            LIMIT 1
                            """,
                            (dedupe_key,),
                        )
                        existing = look_cur.fetchone()
                    if existing is None:
                        raise
                    return int(existing[0])
                else:
                    row = cur.fetchone()
                    conn.commit()
                    return int(row[0])

    def has_recent_or_active_dedupe(
        self,
        dedupe_key: str,
        *,
        since: datetime,
    ) -> bool:
        """Return True if ``dedupe_key`` is active or already fired since ``since``.

        ``enqueue(dedupe_key=...)`` only dedupes queued/claimed rows. A
        second HeartbeatRail can therefore enqueue the same cadence
        handler seconds after the first one completes. Recurring ticks
        use this read-side guard to coalesce same-window pulses across
        rails without permanently reserving the dedupe key.
        """
        if not dedupe_key:
            return False
        since_ts = since.astimezone(UTC)
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM work_jobs
                WHERE dedupe_key = %s
                  AND (
                    status IN ('queued', 'claimed')
                    OR enqueued_at > %s
                  )
                LIMIT 1
                """,
                (dedupe_key, since_ts),
            )
            return cur.fetchone() is not None

    # ------------------------------------------------------------------
    # Claim / complete / fail
    # ------------------------------------------------------------------

    def claim(self, worker_id: str, *, limit: int = 1) -> list[Job]:
        """Atomically claim up to ``limit`` due jobs and return them.

        Uses ``SELECT ... FOR UPDATE SKIP LOCKED`` so concurrent workers
        never see the same row. Rows currently locked by another claim
        in flight are silently skipped; the claimer takes the next free
        row in run_after / id order.
        """
        if limit <= 0:
            return []
        now = _now_utc()
        with self._pool.connection() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE work_jobs
                    SET status = 'claimed',
                        claimed_at = %s,
                        claimed_by = %s,
                        attempt = attempt + 1
                    WHERE id IN (
                        SELECT id FROM work_jobs
                        WHERE status = 'queued' AND run_after <= %s
                        ORDER BY run_after ASC, id ASC
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING id, handler_name, payload_json, attempt,
                              max_attempts, dedupe_key, enqueued_at,
                              run_after, claimed_at, claimed_by
                    """,
                    (now, worker_id, now, limit),
                )
                rows = cur.fetchall()
            conn.commit()

        return [self._row_to_job(row, status=JobStatus.CLAIMED) for row in rows]

    def complete(self, job_id: JobId) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE work_jobs
                SET status = 'done',
                    finished_at = %s,
                    last_error = NULL
                WHERE id = %s
                """,
                (_now_utc(), int(job_id)),
            )

    def fail(
        self,
        job_id: JobId,
        error: str,
        *,
        retry: bool = True,
    ) -> None:
        """Mark a claimed job as failed. May retry with exponential backoff.

        If ``retry=False`` or the job has exhausted its attempts, it
        moves to the ``failed`` terminal state. Otherwise it returns to
        ``queued`` with ``run_after`` bumped per the retry policy.

        When a retry would re-acquire a ``dedupe_key`` that a peer has
        already replaced (peer enqueued a new row after this one fell
        out of ``claimed``), the partial unique index raises
        ``UniqueViolation``. We translate that into a terminal-failed
        state so the late-fail never resurrects a finished slot
        (mirrors the sqlite #1052 behaviour).
        """
        error_text = (error or "")[:8192]
        now_dt = _now_utc()

        with self._pool.connection() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, attempt, max_attempts "
                    "FROM work_jobs WHERE id = %s FOR UPDATE",
                    (int(job_id),),
                )
                row = cur.fetchone()
                if row is None:
                    conn.rollback()
                    return
                status_raw, attempt, max_attempts = (
                    str(row[0]), int(row[1]), int(row[2]),
                )
                if status_raw != JobStatus.CLAIMED.value:
                    conn.rollback()
                    return

                def _mark_terminal_failed(cursor) -> None:
                    cursor.execute(
                        """
                        UPDATE work_jobs
                        SET status = 'failed',
                            finished_at = %s,
                            last_error = %s
                        WHERE id = %s
                        """,
                        (now_dt, error_text, int(job_id)),
                    )

                if not retry or attempt >= max_attempts:
                    _mark_terminal_failed(cur)
                    conn.commit()
                    return

                delay = self.retry_policy(attempt)
                next_run = now_dt + delay
                try:
                    cur.execute(
                        """
                        UPDATE work_jobs
                        SET status = 'queued',
                            run_after = %s,
                            last_error = %s,
                            claimed_at = NULL,
                            claimed_by = NULL
                        WHERE id = %s
                        """,
                        (next_run, error_text, int(job_id)),
                    )
                except Exception as exc:  # noqa: BLE001
                    if not _is_unique_violation(exc):
                        raise
                    # Dedupe-key collision: a peer has already enqueued
                    # a fresh replacement while this row was claimed.
                    # Roll back the failed retry attempt and mark this
                    # row terminal-failed instead so the late retry
                    # never resurrects a finished slot.
                    conn.rollback()
                    logger.warning(
                        "JobQueue.fail: retry for job %s hit a dedupe_key "
                        "collision; marking failed instead",
                        job_id,
                    )
                    with conn.cursor() as term_cur:
                        _mark_terminal_failed(term_cur)
                    conn.commit()
                    return
            conn.commit()

    def recover_orphaned_claims(self) -> tuple[int, int]:
        """Reset every ``claimed`` row back to ``queued`` (#1071).

        Called once at rail-daemon startup. The new daemon owns no
        in-flight claims, so any row still in ``claimed`` was abandoned
        by a previous process that crashed or was killed mid-handler.
        Without this, the dedupe unique index (which covers
        ``status IN ('queued','claimed')``) keeps the orphan's
        dedupe_key permanently reserved, and every subsequent
        ``enqueue(dedupe_key=...)`` short-circuits to the orphan's id —
        silently blocking the cadence handler from ever firing again.

        Two-step recovery:
          1. UPDATE every ``claimed`` row back to ``queued`` with
             ``run_after = now()`` and rewind attempt by 1 since no
             handler body ever ran.
          2. Collapse the *legacy* cadence backlog only: rows with
             ``dedupe_key IS NULL`` whose ``handler_name`` was just
             requeued in step 1 *and* has more than one queued row
             remaining. Pre-#1052 rows had no dedupe_key so the
             previous daemon's cadence ticks accumulated thousands of
             identical session.health_sweep / task_assignment.sweep
             rows in ``claimed``; collapsing those keeps the worker
             pool from grinding through the legacy pile before it can
             fire freshly-scheduled handlers.

             Critically, this prune is scoped to:
               * the handlers actually recovered by *this* invocation,
                 so a stale queued backlog from a healthy queue is left
                 alone (#1822); and
               * NULL-``dedupe_key`` rows only, so legitimate distinct
                 payload/dedupe-keyed jobs are never collapsed (#1822).

        Returns ``(recovered, pruned)`` — the count of orphaned-claim
        rows we requeued, and the count of duplicate queued rows we
        dropped during the same boot pass.
        """
        now = _now_utc()
        with self._pool.connection() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                # Step 1 — capture the handler set we're recovering
                # *before* we mutate ``status`` so the prune in step 2
                # can scope to "handlers this boot just recovered".
                # Without the capture-then-update split, the prune
                # would see every queued handler (including untouched
                # ones) and drop legitimate work — the #1822
                # regression.
                cur.execute(
                    """
                    SELECT DISTINCT handler_name
                    FROM work_jobs
                    WHERE status = 'claimed'
                    """,
                )
                recovered_handlers = [row[0] for row in cur.fetchall()]

                cur.execute(
                    """
                    UPDATE work_jobs
                    SET status = 'queued',
                        claimed_at = NULL,
                        claimed_by = NULL,
                        run_after = %s,
                        attempt = CASE
                            WHEN attempt > 0 THEN attempt - 1
                            ELSE 0
                        END
                    WHERE status = 'claimed'
                    """,
                    (now,),
                )
                recovered = int(cur.rowcount or 0)

                pruned = 0
                if recovered_handlers:
                    # Step 2 — collapse the legacy NULL-dedupe-key
                    # backlog for the handlers we just recovered. We
                    # keep the newest row per handler (so the cadence
                    # can still fire) and drop the older duplicates.
                    # Distinct dedupe_keyed rows are filtered out by
                    # the ``dedupe_key IS NULL`` clause, so payload-
                    # differentiated work survives untouched.
                    cur.execute(
                        """
                        DELETE FROM work_jobs
                        WHERE status = 'queued'
                          AND dedupe_key IS NULL
                          AND handler_name = ANY(%s)
                          AND id NOT IN (
                            SELECT MAX(id) FROM work_jobs
                            WHERE status = 'queued'
                              AND dedupe_key IS NULL
                              AND handler_name = ANY(%s)
                            GROUP BY handler_name
                          )
                        """,
                        (recovered_handlers, recovered_handlers),
                    )
                    pruned = int(cur.rowcount or 0)
            conn.commit()
        return recovered, pruned

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def get(self, job_id: JobId) -> Job | None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, handler_name, payload_json, attempt, max_attempts,
                       dedupe_key, enqueued_at, run_after, claimed_at,
                       claimed_by, status
                FROM work_jobs WHERE id = %s
                """,
                (int(job_id),),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_job(row, status=JobStatus(row[10]))

    def get_last_error(self, job_id: JobId) -> str | None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT last_error FROM work_jobs WHERE id = %s",
                (int(job_id),),
            )
            row = cur.fetchone()
        return None if row is None else row[0]

    def stats(self) -> QueueStats:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status, COUNT(*) FROM work_jobs GROUP BY status",
            )
            rows = cur.fetchall()
        counts = {
            JobStatus.QUEUED: 0,
            JobStatus.CLAIMED: 0,
            JobStatus.DONE: 0,
            JobStatus.FAILED: 0,
        }
        for status, count in rows:
            try:
                counts[JobStatus(status)] = int(count)
            except ValueError:
                continue
        return QueueStats(
            queued=counts[JobStatus.QUEUED],
            claimed=counts[JobStatus.CLAIMED],
            done=counts[JobStatus.DONE],
            failed=counts[JobStatus.FAILED],
        )

    def retry_failed(self, job_id: JobId) -> Job:
        """Reset a failed job to ``queued`` so workers can pick it up.

        Resets the attempt count to zero and clears
        ``claimed_at``/``claimed_by``/``finished_at``/``last_error``.
        ``run_after`` is bumped to ``now`` so the job is eligible
        immediately. Raises ``LookupError`` if the job is missing and
        ``ValueError`` if it isn't currently in the failed state (#803).
        """
        job = self.get(job_id)
        if job is None:
            raise LookupError(f"Job {job_id} not found")
        if job.status is not JobStatus.FAILED:
            raise ValueError(
                f"Job {job_id} is {job.status.value}, not failed — refusing to retry."
            )
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE work_jobs
                SET status = 'queued',
                    attempt = 0,
                    claimed_at = NULL,
                    claimed_by = NULL,
                    finished_at = NULL,
                    last_error = NULL,
                    run_after = %s
                WHERE id = %s
                """,
                (_now_utc(), int(job_id)),
            )
        refreshed = self.get(job_id)
        if refreshed is None:  # pragma: no cover - UPDATE just succeeded
            raise LookupError(f"Job {job_id} disappeared during retry")
        return refreshed

    def purge(self, status: JobStatus) -> int:
        """Bulk-delete jobs in a terminal ``status``. Returns count.

        Only ``done`` and ``failed`` are accepted; non-terminal states
        raise ``ValueError`` so callers can't accidentally drop live
        work via the public API.
        """
        if status not in (JobStatus.DONE, JobStatus.FAILED):
            raise ValueError(
                f"purge only accepts DONE/FAILED, got {status.value}",
            )
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM work_jobs WHERE status = %s",
                (status.value,),
            )
            return int(cur.rowcount or 0)

    def handler_counts(self, *, limit: int = 10) -> list[tuple[str, int]]:
        """Top handlers by current row count, descending. (#803)"""
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT handler_name, COUNT(*)
                FROM work_jobs
                GROUP BY handler_name
                ORDER BY COUNT(*) DESC
                LIMIT %s
                """,
                (int(limit),),
            )
            rows = cur.fetchall()
        return [(str(row[0]), int(row[1])) for row in rows]

    def find_stuck_claims(self, *, limit: int = 1000) -> list[Job]:
        """Return all claimed jobs with a non-null ``claimed_at`` (#1049).

        The caller decides what "stuck" means by comparing
        ``claimed_at`` against a per-handler cutoff — the queue itself
        doesn't know handler timeouts. Returned oldest-claimed-first so
        a bounded ``limit`` still surfaces the actively-blocking
        entries.
        """
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, handler_name, payload_json, attempt, max_attempts,
                       dedupe_key, enqueued_at, run_after, claimed_at,
                       claimed_by, status
                FROM work_jobs
                WHERE status = 'claimed' AND claimed_at IS NOT NULL
                ORDER BY claimed_at ASC, id ASC
                LIMIT %s
                """,
                (int(limit),),
            )
            rows = cur.fetchall()
        return [self._row_to_job(row, status=JobStatus(row[10])) for row in rows]

    def list_jobs(
        self,
        *,
        status: JobStatus | None = None,
        limit: int = 50,
    ) -> list[Job]:
        params: list[Any] = []
        where = ""
        if status is not None:
            where = "WHERE status = %s"
            params.append(status.value)
        params.append(int(limit))
        sql = f"""
            SELECT id, handler_name, payload_json, attempt, max_attempts,
                   dedupe_key, enqueued_at, run_after, claimed_at,
                   claimed_by, status
            FROM work_jobs
            {where}
            ORDER BY id DESC
            LIMIT %s
        """
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
        return [self._row_to_job(row, status=JobStatus(row[10])) for row in rows]

    # ------------------------------------------------------------------
    # Row decoding
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_job(row: tuple, *, status: JobStatus) -> Job:
        """Convert a SELECT-* row into a :class:`Job`.

        Column order MUST match the SELECT lists in :meth:`get`,
        :meth:`list_jobs`, :meth:`claim`, and :meth:`find_stuck_claims`.
        """
        payload_raw = row[2]
        if payload_raw is None:
            payload: dict[str, Any] = {}
        elif isinstance(payload_raw, dict):
            # psycopg returns jsonb columns already decoded.
            payload = dict(payload_raw)
        elif isinstance(payload_raw, (bytes, bytearray)):
            import json as _json

            try:
                decoded = _json.loads(payload_raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                decoded = {}
            payload = decoded if isinstance(decoded, dict) else {}
        elif isinstance(payload_raw, str):
            import json as _json

            try:
                decoded = _json.loads(payload_raw)
            except ValueError:
                decoded = {}
            payload = decoded if isinstance(decoded, dict) else {}
        else:
            payload = {}

        now = _now_utc()
        return Job(
            id=int(row[0]),
            handler_name=str(row[1]),
            payload=payload,
            attempt=int(row[3]),
            max_attempts=int(row[4]),
            dedupe_key=row[5],
            enqueued_at=_ensure_utc(row[6]) or now,
            run_after=_ensure_utc(row[7]) or now,
            claimed_at=_ensure_utc(row[8]),
            claimed_by=row[9],
            status=status,
        )
