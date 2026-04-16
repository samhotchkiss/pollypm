"""Worker pool that drains the durable job queue.

A ``JobWorkerPool`` owns N background threads. Each thread short-polls
``JobQueue.claim`` for work, resolves the handler via a registry, invokes
it with the job's payload, and calls ``complete()`` or ``fail()``.

Per-job timeout is enforced cooperatively: if the handler runs past its
deadline the worker marks the job failed and moves on (the handler may
still be running in its own thread — hard-kill is future work).

Metrics per handler:
  * ``jobs_claimed`` — total jobs seen
  * ``jobs_completed`` — total jobs that returned successfully
  * ``jobs_failed`` — total jobs that raised or timed out
  * ``avg_duration_ms`` — exponential-moving average of runtime

The registry dependency is pluggable (see issue #163); for tests a plain
dict ``{handler_name: HandlerSpec}`` is fine.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


__all__ = [
    "HandlerRegistryProtocol",
    "HandlerSpec",
    "JobWorkerPool",
    "PoolMetrics",
    "WorkerMetrics",
]


logger = logging.getLogger(__name__)


HandlerCallable = Callable[[dict[str, Any]], Any]


@dataclass(slots=True)
class HandlerSpec:
    """Registered handler metadata.

    ``timeout_seconds`` is the per-invocation deadline. ``max_attempts`` is
    passed on to the queue when this handler's jobs are retried (the queue's
    own default applies when not set per-handler, but retry behavior lives
    in ``JobQueue.fail`` — we record it here for observability).
    """

    name: str
    handler: HandlerCallable
    max_attempts: int = 3
    timeout_seconds: float = 30.0
    retry_backoff: str = "exponential"


class HandlerRegistryProtocol(Protocol):
    def get(self, name: str) -> HandlerSpec | None: ...


@dataclass(slots=True)
class WorkerMetrics:
    jobs_claimed: int = 0
    jobs_completed: int = 0
    jobs_failed: int = 0
    total_duration_ms: float = 0.0

    def record_outcome(self, *, success: bool, duration_ms: float) -> None:
        self.jobs_claimed += 1
        if success:
            self.jobs_completed += 1
        else:
            self.jobs_failed += 1
        self.total_duration_ms += duration_ms

    @property
    def avg_duration_ms(self) -> float:
        if self.jobs_claimed == 0:
            return 0.0
        return self.total_duration_ms / self.jobs_claimed


@dataclass(slots=True)
class PoolMetrics:
    per_handler: dict[str, WorkerMetrics] = field(default_factory=dict)

    def for_handler(self, name: str) -> WorkerMetrics:
        metrics = self.per_handler.get(name)
        if metrics is None:
            metrics = WorkerMetrics()
            self.per_handler[name] = metrics
        return metrics

    def snapshot(self) -> dict[str, dict[str, float]]:
        return {
            name: {
                "jobs_claimed": m.jobs_claimed,
                "jobs_completed": m.jobs_completed,
                "jobs_failed": m.jobs_failed,
                "avg_duration_ms": m.avg_duration_ms,
            }
            for name, m in self.per_handler.items()
        }


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------


class JobWorkerPool:
    """Run N background workers that drain a ``JobQueue``.

    Example::

        registry = {"sweep": HandlerSpec("sweep", my_fn, timeout_seconds=10)}
        pool = JobWorkerPool(queue, registry=registry)
        pool.start(concurrency=4)
        ...
        pool.stop(timeout=5)
    """

    def __init__(
        self,
        queue,
        *,
        registry: HandlerRegistryProtocol | dict[str, HandlerSpec],
        poll_interval: float = 0.2,
        worker_name_prefix: str = "pollypm-jobworker",
    ) -> None:
        self.queue = queue
        self._registry = registry
        self.poll_interval = poll_interval
        self.worker_name_prefix = worker_name_prefix

        self._threads: list[threading.Thread] = []
        self._stop_event = threading.Event()
        self._metrics = PoolMetrics()
        self._metrics_lock = threading.Lock()
        self._started = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, *, concurrency: int = 4) -> None:
        if concurrency <= 0:
            raise ValueError("concurrency must be >= 1")
        with self._lock:
            if self._started:
                raise RuntimeError("JobWorkerPool already started")
            self._stop_event.clear()
            self._threads = [
                threading.Thread(
                    target=self._run,
                    name=f"{self.worker_name_prefix}-{i}",
                    args=(f"{self.worker_name_prefix}-{i}",),
                    daemon=True,
                )
                for i in range(concurrency)
            ]
            for t in self._threads:
                t.start()
            self._started = True

    def stop(self, *, timeout: float = 10.0) -> None:
        """Signal workers to stop and wait up to ``timeout`` seconds.

        Workers finish their current job before exiting. Any thread still
        alive after ``timeout`` is left as a daemon — callers should treat
        this as best-effort.
        """
        with self._lock:
            if not self._started:
                return
            self._stop_event.set()
            threads = list(self._threads)
            self._threads = []
            self._started = False
        deadline = time.monotonic() + max(0.0, timeout)
        for t in threads:
            remaining = max(0.0, deadline - time.monotonic())
            t.join(timeout=remaining)
            if t.is_alive():
                logger.warning("JobWorkerPool: thread %s did not stop within timeout", t.name)

    @property
    def is_running(self) -> bool:
        return self._started and not self._stop_event.is_set()

    @property
    def metrics(self) -> PoolMetrics:
        return self._metrics

    # ------------------------------------------------------------------
    # Worker loop
    # ------------------------------------------------------------------

    def _run(self, worker_id: str) -> None:
        logger.debug("JobWorkerPool: worker %s starting", worker_id)
        while not self._stop_event.is_set():
            try:
                batch = self.queue.claim(worker_id, limit=1)
            except Exception as exc:  # noqa: BLE001
                logger.exception("JobWorkerPool: claim failed for %s: %s", worker_id, exc)
                if self._stop_event.wait(self.poll_interval):
                    break
                continue

            if not batch:
                if self._stop_event.wait(self.poll_interval):
                    break
                continue

            for job in batch:
                self._run_one(job)

        logger.debug("JobWorkerPool: worker %s stopping", worker_id)

    def _lookup_handler(self, name: str) -> HandlerSpec | None:
        if isinstance(self._registry, dict):
            return self._registry.get(name)
        return self._registry.get(name)

    def _run_one(self, job) -> None:
        spec = self._lookup_handler(job.handler_name)
        if spec is None:
            # No handler — fail permanently with a clear message.
            self.queue.fail(
                job.id,
                f"No handler registered for '{job.handler_name}'",
                retry=False,
            )
            with self._metrics_lock:
                self._metrics.for_handler(job.handler_name).record_outcome(
                    success=False, duration_ms=0.0
                )
            return

        start = time.monotonic()
        try:
            result_holder: dict[str, Any] = {}
            exc_holder: dict[str, BaseException] = {}

            def _invoke() -> None:
                try:
                    result_holder["value"] = spec.handler(dict(job.payload))
                except BaseException as e:  # noqa: BLE001
                    exc_holder["err"] = e

            t = threading.Thread(
                target=_invoke,
                name=f"{self.worker_name_prefix}-handler-{job.id}",
                daemon=True,
            )
            t.start()
            t.join(timeout=spec.timeout_seconds)
            if t.is_alive():
                # Timeout — mark failed (with retry) and move on.
                elapsed_ms = (time.monotonic() - start) * 1000
                self.queue.fail(
                    job.id,
                    f"Handler {spec.name!r} exceeded timeout of {spec.timeout_seconds}s",
                    retry=True,
                )
                with self._metrics_lock:
                    self._metrics.for_handler(spec.name).record_outcome(
                        success=False, duration_ms=elapsed_ms
                    )
                return

            if "err" in exc_holder:
                err = exc_holder["err"]
                tb = "".join(traceback.format_exception(type(err), err, err.__traceback__))
                elapsed_ms = (time.monotonic() - start) * 1000
                self.queue.fail(job.id, tb, retry=True)
                with self._metrics_lock:
                    self._metrics.for_handler(spec.name).record_outcome(
                        success=False, duration_ms=elapsed_ms
                    )
                return

            elapsed_ms = (time.monotonic() - start) * 1000
            self.queue.complete(job.id)
            with self._metrics_lock:
                self._metrics.for_handler(spec.name).record_outcome(
                    success=True, duration_ms=elapsed_ms
                )

        except Exception as exc:  # noqa: BLE001
            # Defensive — bookkeeping or queue interaction failed.
            logger.exception(
                "JobWorkerPool: unexpected error running job %s (%s): %s",
                job.id, job.handler_name, exc,
            )
            try:
                self.queue.fail(job.id, traceback.format_exc(), retry=True)
            except Exception:  # noqa: BLE001
                pass
            elapsed_ms = (time.monotonic() - start) * 1000
            with self._metrics_lock:
                self._metrics.for_handler(job.handler_name).record_outcome(
                    success=False, duration_ms=elapsed_ms
                )
