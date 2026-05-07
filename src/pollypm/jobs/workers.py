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

import concurrent.futures
import logging
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from pollypm.storage.sqlite_pragmas import (
    is_closed_database_error,
    is_database_locked_error,
)


__all__ = [
    "HandlerRegistryProtocol",
    "HandlerSpec",
    "JobWorkerPool",
    "PoolMetrics",
    "WorkerMetrics",
    "_is_database_locked_error",
]


def _is_closed_db_error(exc: BaseException) -> bool:
    """True iff ``exc`` looks like SQLite's closed-connection error.

    The cockpit hits this whenever some other code path closes the
    JobQueue's connection underneath a live worker — the canonical
    cause is ``pm migrate --apply`` archiving a per-project DB the
    cockpit was still pointing at (#1006). Treating the symptom as a
    clean-shutdown signal stops the worker pool tight-looping on
    full-traceback log spam, which is what was actually killing
    rail_daemon in the production trace.
    """
    return is_closed_database_error(exc)


def _is_database_locked_error(exc: BaseException) -> bool:
    """True iff ``exc`` is SQLite's transient ``database is locked``.

    Different from :func:`_is_closed_db_error` (#1006) — that one is a
    permanent ``ProgrammingError: Cannot operate on a closed database``
    and there is nothing to retry. ``database is locked`` is
    ``OperationalError`` and means the DB was busy past the
    ``busy_timeout`` window: the operation has not been performed but
    the connection is still alive and the next attempt will likely
    succeed once the contending writer commits.

    Symptom is alert ``#67108 critical error_log/critical_error:
    JobWorkerPool: unexpected error running job ... (session.health_sweep):
    database is locked``. We retry the handler invocation a small
    number of times before falling through to the regular ``fail()``
    path — far less disruptive than escalating every transient lock
    to a critical alert.
    """
    return is_database_locked_error(exc)


# #1018 — exponential backoff for the database-locked retry. Three
# attempts at 0.1 s / 0.5 s / 2.0 s (total ~2.6 s ceiling) gives the
# competing writer time to commit, while keeping the worker thread
# from stalling past the next @every 10 s sweep window.
_DB_LOCK_RETRY_BACKOFF: tuple[float, ...] = (0.1, 0.5, 2.0)


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
        poll_interval: float = 1.0,
        worker_name_prefix: str = "pollypm-jobworker",
    ) -> None:
        # ``poll_interval`` is the short-poll cadence when the queue is
        # empty. The default of 0.2 s (5 polls/worker/s × 4 workers ≈ 20
        # SQLite queries/s) pinned the rail daemon at 160% CPU on an
        # otherwise-idle system on 2026-04-20. Bumping to 1.0 s brings
        # idle CPU down ~5× with worst-case pickup latency ~1 s — well
        # inside the @every 10 s cadence of the fastest recurring
        # handler (session.health_sweep). Tests that need tighter
        # polling can pass ``poll_interval`` explicitly.
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

        When a worker thread fails to stop within the deadline we also
        emit a ``worker.thread_leaked`` audit event (#1370) so the fleet
        can quantify how often this is firing without grepping
        ``errors.log``.
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
                self._emit_thread_leaked(t.name, timeout)

    @staticmethod
    def _emit_thread_leaked(thread_name: str, timeout: float) -> None:
        """Best-effort audit hook for #1370.

        Audit emit is intentionally lazy-imported and exception-swallowed:
        the shutdown path must never raise from a telemetry hook.
        """
        try:
            from pollypm.audit.log import EVENT_WORKER_THREAD_LEAKED
            from pollypm.audit.log import emit as _audit_emit

            _audit_emit(
                event=EVENT_WORKER_THREAD_LEAKED,
                project="",
                subject=thread_name,
                actor="system",
                status="warn",
                metadata={"timeout_seconds": float(timeout)},
            )
        except Exception:  # noqa: BLE001 — audit must not break stop()
            pass

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
        # #1370 — per-worker single-threaded executor reused across
        # job invocations. Pre-fix, ``_run_one`` spawned a fresh
        # ``threading.Thread(daemon=True)`` per attempt and abandoned
        # the reference on timeout, leaking one thread per timed-out
        # handler attempt (and up to four per job once the lock-retry
        # loop kicked in). With a single-slot executor the worker has
        # at most one in-flight handler thread at a time; on timeout
        # the executor's worker keeps running the dead handler in the
        # background but no *new* thread is created — the next attempt
        # reuses the next executor slot once the old future drops the
        # reservation. The executor is shut down with
        # ``cancel_futures=True`` and ``wait=False`` when the worker
        # exits so a wedged handler can't block ``pool.stop()``.
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"{self.worker_name_prefix}-handler",
        )
        try:
            while not self._stop_event.is_set():
                try:
                    batch = self.queue.claim(worker_id, limit=1)
                except Exception as exc:  # noqa: BLE001
                    if _is_closed_db_error(exc):
                        self._handle_closed_db(worker_id, "claim")
                        break
                    if _is_database_locked_error(exc):
                        # #1018 — transient WAL contention on claim; back
                        # off briefly and retry on the next poll. Don't log
                        # a full traceback (would spam ``errors.log`` and,
                        # via the heartbeat alert pipeline, raise a
                        # ``critical_error`` for a recoverable condition).
                        logger.debug(
                            "JobWorkerPool: claim hit transient lock for %s; "
                            "backing off",
                            worker_id,
                        )
                        if self._stop_event.wait(self.poll_interval):
                            break
                        continue
                    logger.exception("JobWorkerPool: claim failed for %s: %s", worker_id, exc)
                    if self._stop_event.wait(self.poll_interval):
                        break
                    continue

                if not batch:
                    if self._stop_event.wait(self.poll_interval):
                        break
                    continue

                for job in batch:
                    self._run_one(job, executor)
                    if self._stop_event.is_set():
                        break
        finally:
            # ``cancel_futures=True`` drops anything still queued behind
            # an in-flight handler; ``wait=False`` lets a wedged handler
            # die at process exit instead of pinning the worker thread
            # in ``executor.__exit__``. Both are deliberate — the
            # alternative is exactly the leak this PR is fixing.
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception:  # noqa: BLE001 — never block worker exit
                logger.debug(
                    "JobWorkerPool: worker %s executor shutdown raised",
                    worker_id,
                    exc_info=True,
                )

        logger.debug("JobWorkerPool: worker %s stopping", worker_id)

    def _handle_closed_db(self, worker_id: str, operation: str) -> None:
        """Log once and trip the stop event when the DB connection is gone.

        Called when claim/complete/fail raises ``sqlite3.ProgrammingError:
        Cannot operate on a closed database``. Workers that hit this can
        never make progress against the dead connection — re-raising on
        every poll burns CPU and floods ``errors.log`` with full
        tracebacks (see #1006). Setting ``_stop_event`` lets sibling
        workers exit on their next short-poll without waiting for the
        join timeout to lapse.
        """
        # Best-effort: only the first worker to notice gets the warning,
        # so we don't print N copies of the same line. Any worker setting
        # the event is sufficient — Event.set() is idempotent.
        already_signalled = self._stop_event.is_set()
        self._stop_event.set()
        if not already_signalled:
            logger.warning(
                "JobWorkerPool: %s by %s hit closed-DB; stopping pool. "
                "Likely cause: another process (e.g. `pm migrate --apply`) "
                "closed the queue connection underneath us. Restart the "
                "cockpit to recover.",
                operation, worker_id,
            )

    def _lookup_handler(self, name: str) -> HandlerSpec | None:
        if isinstance(self._registry, dict):
            return self._registry.get(name)
        return self._registry.get(name)

    def _run_one(self, job, executor: concurrent.futures.ThreadPoolExecutor) -> None:
        spec = self._lookup_handler(job.handler_name)
        if spec is None:
            # No handler — fail permanently with a clear message.
            try:
                self.queue.fail(
                    job.id,
                    f"No handler registered for '{job.handler_name}'",
                    retry=False,
                )
            except Exception as exc:  # noqa: BLE001
                if _is_closed_db_error(exc):
                    self._handle_closed_db("worker", "fail")
                    return
                raise
            with self._metrics_lock:
                self._metrics.for_handler(job.handler_name).record_outcome(
                    success=False, duration_ms=0.0
                )
            return

        start = time.monotonic()
        try:
            # #1018 — retry-on-lock loop. SQLite ``database is locked``
            # is a transient WAL contention, not a programming bug, so
            # we re-invoke the handler with exponential backoff before
            # falling through to the regular ``fail()`` path. Without
            # this, every transient lock during ``session.health_sweep``
            # bubbled up as a ``critical_error`` alert (#67108).
            #
            # #1370 — invocations route through a single-slot
            # ``ThreadPoolExecutor`` owned by the worker thread, not a
            # fresh ``threading.Thread`` per attempt. ``future.result()``
            # gives the same per-attempt timeout semantics as
            # ``Thread.join(timeout=)`` without spawning a new OS thread
            # each retry. On timeout we abandon the future (the
            # underlying handler keeps running in the executor's worker
            # thread) and bail out of the retry loop — re-submitting
            # while the previous attempt is still alive would either
            # block on the executor's single slot or, pre-fix, double
            # up handler threads against the same job.
            attempts = 1 + len(_DB_LOCK_RETRY_BACKOFF)
            timed_out = False
            last_err: BaseException | None = None
            for attempt_index in range(attempts):
                payload_copy = dict(job.payload)
                try:
                    future = executor.submit(spec.handler, payload_copy)
                except RuntimeError:
                    # Executor was shut down underneath us (pool stopping).
                    # Treat as a stop signal and exit without bookkeeping —
                    # ``stop()`` already accounts for in-flight work.
                    return

                try:
                    future.result(timeout=spec.timeout_seconds)
                    last_err = None
                except concurrent.futures.TimeoutError:
                    # Handler is still running on the executor's worker
                    # thread. Abandon the future — the executor will
                    # not accept a new submit() until this one finishes
                    # (single-slot), and re-trying while it's stuck
                    # would just block here on the next submit(). Mark
                    # timed_out and break so the regular timeout-fail
                    # path runs.
                    future.cancel()  # no-op once running, but cheap.
                    timed_out = True
                    break
                except BaseException as e:  # noqa: BLE001
                    last_err = e

                if last_err is None or not _is_database_locked_error(last_err):
                    break

                # Lock-retry path. Last attempt falls through with the
                # error preserved so the regular fail() route runs.
                if attempt_index >= len(_DB_LOCK_RETRY_BACKOFF):
                    break
                backoff_seconds = _DB_LOCK_RETRY_BACKOFF[attempt_index]
                logger.debug(
                    "JobWorkerPool: job %s (%s) hit database-locked "
                    "(attempt %d/%d); retrying after %.2fs",
                    job.id, job.handler_name,
                    attempt_index + 1, attempts, backoff_seconds,
                )
                # ``Event.wait`` returns True if stop_event was set —
                # honour shutdown by breaking out before the next try.
                if self._stop_event.wait(backoff_seconds):
                    break

            if timed_out:
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

            if last_err is not None:
                err = last_err
                tb = "".join(traceback.format_exception(type(err), err, err.__traceback__))
                elapsed_ms = (time.monotonic() - start) * 1000
                # #1018 — log lock-exhaustion at WARNING (not ERROR /
                # critical_error). The job is queued for a normal retry;
                # the operator sees one warn line per exhaustion, not a
                # full traceback escalated to a critical alert.
                if _is_database_locked_error(err):
                    logger.warning(
                        "JobWorkerPool: job %s (%s) gave up after "
                        "%d database-locked retries — queue.fail with "
                        "retry=True for the regular backoff path",
                        job.id, job.handler_name, attempts,
                    )
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
            if _is_closed_db_error(exc):
                # Don't log a full traceback — closed-DB errors are
                # operational, not programming bugs, and four workers
                # spamming tracebacks each poll is what zombied
                # rail_daemon in #1006.
                self._handle_closed_db("worker", "complete/fail")
                return
            if _is_database_locked_error(exc):
                # #1018 — queue.complete()/fail() lost the WAL race.
                # Log at warning (not exception) so the heartbeat
                # alert pipeline does not escalate the transient
                # contention to a ``critical_error`` alert.
                logger.warning(
                    "JobWorkerPool: queue bookkeeping for job %s (%s) "
                    "hit transient database-locked: %s",
                    job.id, job.handler_name, exc,
                )
                elapsed_ms = (time.monotonic() - start) * 1000
                with self._metrics_lock:
                    self._metrics.for_handler(job.handler_name).record_outcome(
                        success=False, duration_ms=elapsed_ms
                    )
                return
            logger.exception(
                "JobWorkerPool: unexpected error running job %s (%s): %s",
                job.id, job.handler_name, exc,
            )
            try:
                self.queue.fail(job.id, traceback.format_exc(), retry=True)
            except Exception as fail_exc:  # noqa: BLE001
                if _is_closed_db_error(fail_exc):
                    self._handle_closed_db("worker", "fail")
                    return
            elapsed_ms = (time.monotonic() - start) * 1000
            with self._metrics_lock:
                self._metrics.for_handler(job.handler_name).record_outcome(
                    success=False, duration_ms=elapsed_ms
                )
