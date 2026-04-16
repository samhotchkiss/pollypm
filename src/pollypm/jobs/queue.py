"""SQLite-backed durable job queue.

Schema (see ``storage/state.py`` migration 6)::

    CREATE TABLE work_jobs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      handler_name TEXT NOT NULL,
      payload_json TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'queued',   -- queued|claimed|done|failed
      attempt INTEGER NOT NULL DEFAULT 0,
      max_attempts INTEGER NOT NULL DEFAULT 3,
      dedupe_key TEXT,
      enqueued_at TEXT NOT NULL,
      run_after TEXT NOT NULL,
      claimed_at TEXT,
      claimed_by TEXT,
      finished_at TEXT,
      last_error TEXT
    );

    -- Unique-when-pending dedupe: prevents pileup for dedupe_key + queued/claimed.
    CREATE UNIQUE INDEX idx_work_jobs_dedupe_queued
    ON work_jobs(dedupe_key)
    WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'claimed');

Claiming uses ``UPDATE ... RETURNING`` (SQLite >= 3.35) so the operation is
atomic — two concurrent claim() calls cannot return the same row.
"""

from __future__ import annotations

import json
import random
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable


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


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


class JobQueue:
    """Thread-safe SQLite-backed durable job queue.

    Construction options:

    * ``db_path`` — path to a standalone queue DB. A new connection is
      opened with WAL + a 30s busy timeout.
    * ``connection`` — pre-built ``sqlite3.Connection`` (used by tests and
      when the queue shares the main state DB).

    Exactly one of the two must be provided.
    """

    def __init__(
        self,
        *,
        db_path: Path | str | None = None,
        connection: sqlite3.Connection | None = None,
        default_max_attempts: int = 3,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        if (db_path is None) == (connection is None):
            raise ValueError("JobQueue requires exactly one of db_path or connection")
        self._lock = threading.RLock()
        self._owns_connection = connection is None
        if connection is not None:
            self._conn = connection
        else:
            path = Path(db_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(path), check_same_thread=False, isolation_level=None
            )
            with self._lock:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA busy_timeout=30000")
        self._ensure_schema()
        self.default_max_attempts = default_max_attempts
        self.retry_policy = retry_policy or exponential_backoff()

    def close(self) -> None:
        if self._owns_connection:
            with self._lock:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001
                    pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        """Create the table + indexes if missing — safe for standalone DBs."""
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS work_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    handler_name TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    attempt INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    dedupe_key TEXT,
                    enqueued_at TEXT NOT NULL,
                    run_after TEXT NOT NULL,
                    claimed_at TEXT,
                    claimed_by TEXT,
                    finished_at TEXT,
                    last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_work_jobs_claim
                ON work_jobs(status, run_after, id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_work_jobs_dedupe_queued
                ON work_jobs(dedupe_key)
                WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'claimed');
                """
            )

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

        If a queued-or-claimed job with the same ``dedupe_key`` already exists,
        the existing job's id is returned and no new row is inserted.
        """
        if not handler_name:
            raise ValueError("handler_name is required")
        payload_json = json.dumps(payload or {}, sort_keys=True, default=str)
        run_after_iso = (run_after or datetime.now(UTC)).astimezone(UTC).isoformat()
        max_att = max_attempts if max_attempts is not None else self.default_max_attempts
        now = _now_iso()

        with self._lock:
            if dedupe_key is not None:
                existing = self._conn.execute(
                    """
                    SELECT id FROM work_jobs
                    WHERE dedupe_key = ? AND status IN ('queued', 'claimed')
                    LIMIT 1
                    """,
                    (dedupe_key,),
                ).fetchone()
                if existing is not None:
                    return int(existing[0])

            try:
                cursor = self._conn.execute(
                    """
                    INSERT INTO work_jobs (
                        handler_name, payload_json, status, attempt, max_attempts,
                        dedupe_key, enqueued_at, run_after
                    )
                    VALUES (?, ?, 'queued', 0, ?, ?, ?, ?)
                    """,
                    (handler_name, payload_json, max_att, dedupe_key, now, run_after_iso),
                )
            except sqlite3.IntegrityError:
                # Raced with another enqueue on the same dedupe_key — look up.
                if dedupe_key is None:
                    raise
                existing = self._conn.execute(
                    """
                    SELECT id FROM work_jobs
                    WHERE dedupe_key = ? AND status IN ('queued', 'claimed')
                    LIMIT 1
                    """,
                    (dedupe_key,),
                ).fetchone()
                if existing is None:
                    raise
                return int(existing[0])
            return int(cursor.lastrowid)

    # ------------------------------------------------------------------
    # Claim / complete / fail
    # ------------------------------------------------------------------

    def claim(self, worker_id: str, *, limit: int = 1) -> list[Job]:
        """Atomically claim up to ``limit`` due jobs and return them.

        Uses ``UPDATE ... RETURNING`` so concurrent workers cannot double-claim.
        """
        if limit <= 0:
            return []
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        with self._lock:
            # Two-step claim: pick candidate IDs under a transaction, then
            # UPDATE ... RETURNING to lock them atomically. SQLite serializes
            # writes so concurrent claims can't collide on the same row.
            rows = self._conn.execute(
                """
                UPDATE work_jobs
                SET status = 'claimed', claimed_at = ?, claimed_by = ?, attempt = attempt + 1
                WHERE id IN (
                    SELECT id FROM work_jobs
                    WHERE status = 'queued' AND run_after <= ?
                    ORDER BY run_after ASC, id ASC
                    LIMIT ?
                )
                RETURNING id, handler_name, payload_json, attempt, max_attempts,
                          dedupe_key, enqueued_at, run_after, claimed_at, claimed_by
                """,
                (now, worker_id, now, limit),
            ).fetchall()

        jobs: list[Job] = []
        for row in rows:
            jobs.append(
                Job(
                    id=int(row[0]),
                    handler_name=row[1],
                    payload=json.loads(row[2] or "{}"),
                    attempt=int(row[3]),
                    max_attempts=int(row[4]),
                    dedupe_key=row[5],
                    enqueued_at=_parse_ts(row[6]) or now_dt,
                    run_after=_parse_ts(row[7]) or now_dt,
                    claimed_at=_parse_ts(row[8]),
                    claimed_by=row[9],
                    status=JobStatus.CLAIMED,
                )
            )
        return jobs

    def complete(self, job_id: JobId) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE work_jobs
                SET status = 'done', finished_at = ?, last_error = NULL
                WHERE id = ?
                """,
                (_now_iso(), int(job_id)),
            )

    def fail(
        self,
        job_id: JobId,
        error: str,
        *,
        retry: bool = True,
    ) -> None:
        """Mark a claimed job as failed. May retry with exponential backoff.

        If ``retry=False`` or the job has exhausted its attempts, it moves to
        the ``failed`` terminal state. Otherwise it returns to ``queued`` with
        ``run_after`` bumped per the retry policy.
        """
        error_text = (error or "")[:8192]
        now_dt = datetime.now(UTC)
        with self._lock:
            row = self._conn.execute(
                "SELECT attempt, max_attempts FROM work_jobs WHERE id = ?",
                (int(job_id),),
            ).fetchone()
            if row is None:
                return
            attempt, max_attempts = int(row[0]), int(row[1])

            if not retry or attempt >= max_attempts:
                self._conn.execute(
                    """
                    UPDATE work_jobs
                    SET status = 'failed', finished_at = ?, last_error = ?
                    WHERE id = ?
                    """,
                    (now_dt.isoformat(), error_text, int(job_id)),
                )
                return

            delay = self.retry_policy(attempt)
            next_run = (now_dt + delay).isoformat()
            self._conn.execute(
                """
                UPDATE work_jobs
                SET status = 'queued',
                    run_after = ?,
                    last_error = ?,
                    claimed_at = NULL,
                    claimed_by = NULL
                WHERE id = ?
                """,
                (next_run, error_text, int(job_id)),
            )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def get(self, job_id: JobId) -> Job | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, handler_name, payload_json, attempt, max_attempts,
                       dedupe_key, enqueued_at, run_after, claimed_at, claimed_by, status
                FROM work_jobs WHERE id = ?
                """,
                (int(job_id),),
            ).fetchone()
        if row is None:
            return None
        return Job(
            id=int(row[0]),
            handler_name=row[1],
            payload=json.loads(row[2] or "{}"),
            attempt=int(row[3]),
            max_attempts=int(row[4]),
            dedupe_key=row[5],
            enqueued_at=_parse_ts(row[6]) or datetime.now(UTC),
            run_after=_parse_ts(row[7]) or datetime.now(UTC),
            claimed_at=_parse_ts(row[8]),
            claimed_by=row[9],
            status=JobStatus(row[10]),
        )

    def get_last_error(self, job_id: JobId) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_error FROM work_jobs WHERE id = ?", (int(job_id),)
            ).fetchone()
        return None if row is None else row[0]

    def stats(self) -> QueueStats:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) FROM work_jobs GROUP BY status"
            ).fetchall()
        counts = {JobStatus.QUEUED: 0, JobStatus.CLAIMED: 0, JobStatus.DONE: 0, JobStatus.FAILED: 0}
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

    def list_jobs(
        self,
        *,
        status: JobStatus | None = None,
        limit: int = 50,
    ) -> list[Job]:
        params: list[Any] = []
        where = ""
        if status is not None:
            where = "WHERE status = ?"
            params.append(status.value)
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT id, handler_name, payload_json, attempt, max_attempts,
                       dedupe_key, enqueued_at, run_after, claimed_at, claimed_by, status
                FROM work_jobs
                {where}
                ORDER BY id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [
            Job(
                id=int(row[0]),
                handler_name=row[1],
                payload=json.loads(row[2] or "{}"),
                attempt=int(row[3]),
                max_attempts=int(row[4]),
                dedupe_key=row[5],
                enqueued_at=_parse_ts(row[6]) or datetime.now(UTC),
                run_after=_parse_ts(row[7]) or datetime.now(UTC),
                claimed_at=_parse_ts(row[8]),
                claimed_by=row[9],
                status=JobStatus(row[10]),
            )
            for row in rows
        ]
