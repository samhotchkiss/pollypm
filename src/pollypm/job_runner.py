"""Async job runner for PollyPM internal cron jobs.

The heartbeat sweep is fast and deterministic.  Slow work (LLM calls,
network checks, user-defined cron jobs) gets dispatched here and runs
in parallel on a thread pool.  The heartbeat cron fires every 60s,
does its quick sweep, then submits due jobs and exits without waiting.

Jobs are defined by kind.  Built-in kinds are registered here; plugin
kinds are discovered from the plugin host at runtime.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from pollypm.supervisor import Supervisor

logger = logging.getLogger(__name__)

# Module-level pool shared across heartbeat invocations within the same process.
# When cron spawns a new process each cycle, the pool is fresh — that's fine,
# the previous cycle's threads completed (or will complete) in the old process.
_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()

MAX_WORKERS = 4  # cap parallelism — these are I/O-bound, not CPU-bound


@dataclass(slots=True)
class JobResult:
    kind: str
    success: bool
    message: str = ""
    error: str = ""


# Registry of built-in job executors: kind -> callable(supervisor, payload) -> None
_BUILTIN_EXECUTORS: dict[str, Callable[..., None]] = {}


def register_job(kind: str):
    """Decorator to register a built-in job executor."""
    def decorator(fn: Callable[..., None]) -> Callable[..., None]:
        _BUILTIN_EXECUTORS[kind] = fn
        return fn
    return decorator


# -- Built-in jobs ----------------------------------------------------------

@register_job("heartbeat")
def _run_heartbeat(supervisor: Supervisor, payload: dict[str, Any]) -> None:
    supervisor.run_heartbeat()


@register_job("send_input")
def _run_send_input(supervisor: Supervisor, payload: dict[str, Any]) -> None:
    supervisor.send_input(
        str(payload["session_name"]),
        str(payload["text"]),
        owner=str(payload.get("owner", "pm-bot")),
    )


@register_job("release_lease")
def _run_release_lease(supervisor: Supervisor, payload: dict[str, Any]) -> None:
    supervisor.release_lease(
        str(payload["session_name"]),
        str(payload.get("owner", "human")),
    )


@register_job("knowledge_extract")
def _run_knowledge_extract(supervisor: Supervisor, payload: dict[str, Any]) -> None:
    from pollypm.knowledge_extract import extract_knowledge_once
    extract_knowledge_once(supervisor.config)


@register_job("version_check")
def _run_version_check(supervisor: Supervisor, payload: dict[str, Any]) -> None:
    from pollypm.version_check import check_and_notify
    check_and_notify(supervisor.config.project.root_dir, supervisor.config.project.base_dir)


@register_job("token_ledger_sync")
def _run_token_ledger_sync(supervisor: Supervisor, payload: dict[str, Any]) -> None:
    from pollypm.transcript_ledger import sync_token_ledger_for_config
    samples = sync_token_ledger_for_config(supervisor.config)
    if samples:
        supervisor.store.record_event(
            "heartbeat",
            "token_ledger",
            f"Synced {len(samples)} transcript token sample(s)",
        )


@register_job("gc_maintenance")
def _run_gc_maintenance(supervisor: Supervisor, payload: dict[str, Any]) -> None:
    """Force garbage collection to reclaim leaked FDs and memory."""
    import gc
    gc.collect()


@register_job("inbox_escalation")
def _run_inbox_escalation(supervisor: Supervisor, payload: dict[str, Any]) -> None:
    """Escalate unattended waiting_on_user sessions to the inbox."""
    from pollypm.inbox_escalation import escalate_waiting_sessions
    escalate_waiting_sessions(supervisor.store, supervisor.config.project.root_dir)


@register_job("prune_state")
def _run_prune_state(supervisor: Supervisor, payload: dict[str, Any]) -> None:
    """Prune old events and heartbeat data to prevent unbounded growth."""
    result = supervisor.store.prune_old_data()
    total = sum(result.values())
    if total > 0:
        supervisor.store.record_event(
            "maintenance",
            "prune",
            f"Pruned {result['events']} events, {result['heartbeats']} heartbeat records",
        )


# -- Pool management --------------------------------------------------------

def _get_pool() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="pm-job")
        return _pool


def get_executor(kind: str) -> Callable[..., None] | None:
    """Look up executor for a job kind.  Checks built-ins first, then plugins."""
    if kind in _BUILTIN_EXECUTORS:
        return _BUILTIN_EXECUTORS[kind]
    # Plugin-provided jobs could be looked up here in the future:
    #   from pollypm.plugin_host import get_cron_job_executor
    #   return get_cron_job_executor(kind)
    return None


def submit_job(
    supervisor: Supervisor,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    on_done: Callable[[Future], None] | None = None,
) -> Future | None:
    """Submit a job for async execution on the thread pool.

    Returns the Future, or None if no executor is registered for the kind.
    The caller does NOT need to wait on the Future — fire and forget.
    """
    executor_fn = get_executor(kind)
    if executor_fn is None:
        logger.warning("No executor registered for job kind: %s", kind)
        return None

    pool = _get_pool()

    def _run():
        try:
            executor_fn(supervisor, payload or {})
        except Exception:
            logger.exception("Job %s failed", kind)
            raise

    future = pool.submit(_run)
    if on_done:
        future.add_done_callback(on_done)
    return future


def submit_jobs_parallel(
    supervisor: Supervisor,
    jobs: list[tuple[str, dict[str, Any]]],
) -> list[Future]:
    """Submit multiple jobs for parallel execution.  Fire and forget."""
    futures = []
    for kind, payload in jobs:
        f = submit_job(supervisor, kind, payload)
        if f is not None:
            futures.append(f)
    return futures


def shutdown_pool(wait: bool = False) -> None:
    """Shut down the thread pool.  Called on pm reset or process exit."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.shutdown(wait=wait)
            _pool = None
