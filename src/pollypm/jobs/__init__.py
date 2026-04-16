"""Durable job queue + worker pool for PollyPM.

Public API:

    >>> from pollypm.jobs import Job, JobQueue, JobStatus, QueueStats

The queue is SQLite-backed (``work_jobs`` table) and supports atomic
``claim()`` via ``UPDATE ... RETURNING``, dedupe keys, exponential-backoff
retries, and delayed visibility (``run_after``).
"""

from __future__ import annotations

from pollypm.jobs.queue import (
    Job,
    JobId,
    JobQueue,
    JobStatus,
    QueueStats,
    RetryPolicy,
    exponential_backoff,
)

__all__ = [
    "Job",
    "JobId",
    "JobQueue",
    "JobStatus",
    "QueueStats",
    "RetryPolicy",
    "exponential_backoff",
]
