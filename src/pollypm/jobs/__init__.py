"""Durable job queue + worker pool for PollyPM.

Public API:

    >>> from pollypm.jobs import Job, JobQueue, JobStatus, QueueStats

The queue is SQLite-backed (``work_jobs`` table) and supports atomic
``claim()`` via ``UPDATE ... RETURNING``, dedupe keys, exponential-backoff
retries, and delayed visibility (``run_after``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pollypm.jobs.queue import (
    Job,
    JobId,
    JobQueue,
    JobStatus,
    QueueStats,
    RetryPolicy,
    exponential_backoff,
)

if TYPE_CHECKING:
    from pollypm.jobs.registry import JobHandlerRegistry
    from pollypm.jobs.workers import (
        HandlerRegistryProtocol,
        HandlerSpec,
        JobWorkerPool,
        PoolMetrics,
        WorkerMetrics,
    )


_WORKER_EXPORTS = {
    "HandlerRegistryProtocol",
    "HandlerSpec",
    "JobWorkerPool",
    "JobHandlerRegistry",
    "PoolMetrics",
    "WorkerMetrics",
}


def __getattr__(name: str):
    if name in _WORKER_EXPORTS:
        from importlib import import_module

        if name == "JobHandlerRegistry":
            _registry = import_module("pollypm.jobs.registry")
            return getattr(_registry, name)

        _workers = import_module("pollypm.jobs.workers")

        return getattr(_workers, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "HandlerRegistryProtocol",
    "HandlerSpec",
    "Job",
    "JobHandlerRegistry",
    "JobId",
    "JobQueue",
    "JobStatus",
    "JobWorkerPool",
    "PoolMetrics",
    "QueueStats",
    "RetryPolicy",
    "WorkerMetrics",
    "exponential_backoff",
]
