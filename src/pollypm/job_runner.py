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


def _last_event_ts(
    supervisor: Supervisor, scope: str, subject: str,
) -> datetime | None:
    """Return the timestamp of the most recent ``(scope, subject)`` event.

    #349: replaces the legacy :meth:`StateStore.last_event_at` lookup so the
    rate-limit gate reads from the same table writers now emit to
    (``messages`` via the unified Store). Returns ``None`` when no matching
    event has been recorded yet.
    """
    try:
        rows = supervisor.msg_store.query_messages(
            type="event",
            scope=scope,
            limit=20,
        )
    except Exception:  # noqa: BLE001
        return None
    for row in rows:
        if row.get("subject") != subject:
            continue
        created_at = row.get("created_at")
        if created_at is None:
            continue
        stamp = (
            created_at.isoformat()
            if hasattr(created_at, "isoformat")
            else str(created_at)
        )
        try:
            parsed = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    return None


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
    """Deprecated — knowledge extraction now happens via session_intelligence (Haiku)
    and project_intelligence (Opus). Kept as no-op for backward compatibility."""
    pass


@register_job("session_intelligence_sweep")
def _run_session_intelligence_sweep(supervisor: Supervisor, payload: dict[str, Any]) -> None:
    """Tier 1 sweep: process ALL sessions with new transcript activity.

    Runs at most every 5 minutes. One Haiku call per active session.
    Extracts knowledge entries and activity summaries.
    """
    from datetime import UTC, datetime
    # #349: rate-limit check reads events from the unified ``messages`` table.
    last = _last_event_ts(supervisor, "session_intelligence", "sweep_completed")
    if last is not None:
        age = (datetime.now(UTC) - last).total_seconds()
        if age < 300:  # 5 minutes
            return
    from pollypm.session_intelligence import sweep_all_sessions
    result = sweep_all_sessions(supervisor.config)
    if result["sessions_processed"]:
        from pollypm.plugins_builtin.activity_feed.summaries import activity_summary

        n_sessions = result["sessions_processed"]
        n_entries = result["knowledge_entries"]
        n_summaries = result["summaries"]
        sessions_word = "session" if n_sessions == 1 else "sessions"
        entry_word = "entry" if n_entries == 1 else "entries"
        summary_word = "summary" if n_summaries == 1 else "summaries"
        supervisor.msg_store.append_event(
            scope="session_intelligence",
            sender="session_intelligence",
            subject="sweep_completed",
            payload={
                "message": activity_summary(
                    summary=(
                        f"Processed {n_sessions} {sessions_word}, "
                        f"{n_entries} knowledge {entry_word}, "
                        f"{n_summaries} {summary_word}"
                    ),
                    severity="routine",
                    verb="swept",
                    subject="session_intelligence",
                    sessions_processed=result["sessions_processed"],
                    knowledge_entries=result["knowledge_entries"],
                    summaries=result["summaries"],
                ),
                "sessions_processed": result["sessions_processed"],
                "knowledge_entries": result["knowledge_entries"],
                "summaries": result["summaries"],
            },
        )


@register_job("project_intelligence")
def _run_project_intelligence(supervisor: Supervisor, payload: dict[str, Any]) -> None:
    """Tier 2: Opus rewrites project docs from accumulated Haiku knowledge entries.

    Runs at most every 60 minutes. Only fires if pending knowledge exists.
    """
    from datetime import UTC, datetime
    # #349: rate-limit check reads from the unified ``messages`` table.
    last = _last_event_ts(supervisor, "project_intelligence", "completed")
    if last is not None:
        age = (datetime.now(UTC) - last).total_seconds()
        if age < 3600:  # 1 hour
            return
    from pollypm.knowledge_extract import _all_project_roots
    from pollypm.project_intelligence import run_project_intelligence
    updated = 0
    for project_root in _all_project_roots(supervisor.config):
        if run_project_intelligence(supervisor.config, project_root):
            updated += 1
    if updated:
        from pollypm.plugins_builtin.activity_feed.summaries import activity_summary

        supervisor.msg_store.append_event(
            scope="project_intelligence",
            sender="project_intelligence",
            subject="completed",
            payload={
                "message": activity_summary(
                    summary=(
                        f"Updated docs for {updated} "
                        f"{'project' if updated == 1 else 'projects'}"
                    ),
                    severity="routine",
                    verb="updated",
                    subject="project_intelligence",
                    projects_updated=updated,
                ),
                "projects_updated": updated,
            },
        )


@register_job("version_check")
def _run_version_check(supervisor: Supervisor, payload: dict[str, Any]) -> None:
    from pollypm.version_check import check_and_notify
    check_and_notify(supervisor.config.project.root_dir, supervisor.config.project.base_dir)


@register_job("token_ledger_sync")
def _run_token_ledger_sync(supervisor: Supervisor, payload: dict[str, Any]) -> None:
    from pollypm.transcript_ledger import sync_token_ledger_for_config
    samples = sync_token_ledger_for_config(supervisor.config)
    if samples:
        from pollypm.plugins_builtin.activity_feed.summaries import activity_summary

        supervisor.msg_store.append_event(
            scope="heartbeat",
            sender="heartbeat",
            subject="token_ledger",
            payload={
                "message": activity_summary(
                    summary=(
                        f"Synced {len(samples)} transcript token "
                        f"{'sample' if len(samples) == 1 else 'samples'}"
                    ),
                    severity="routine",
                    verb="synced",
                    subject="token_ledger",
                    samples=len(samples),
                ),
                "samples": len(samples),
            },
        )


@register_job("gc_maintenance")
def _run_gc_maintenance(supervisor: Supervisor, payload: dict[str, Any]) -> None:
    """Force garbage collection and trim tmux scrollback on all panes."""
    import gc
    gc.collect()
    # Trim scrollback on all managed panes to prevent slow session switching
    try:
        for session_name in (
            supervisor.config.project.tmux_session,
            supervisor.storage_closet_session_name(),
        ):
            if not supervisor.tmux.has_session(session_name):
                continue
            for window in supervisor.tmux.list_windows(session_name):
                target = f"{session_name}:{window.name}"
                supervisor.tmux.set_pane_history_limit(target, 200)
    except Exception:  # noqa: BLE001
        pass


@register_job("prune_state")
def _run_prune_state(supervisor: Supervisor, payload: dict[str, Any]) -> None:
    """Prune old events and heartbeat data to prevent unbounded growth."""
    result = supervisor.store.prune_old_data()
    total = sum(result.values())
    if total > 0:
        from pollypm.plugins_builtin.activity_feed.summaries import activity_summary

        events_count = result["events"]
        heartbeats_count = result["heartbeats"]
        events_word = "event" if events_count == 1 else "events"
        heartbeats_word = (
            "heartbeat record" if heartbeats_count == 1 else "heartbeat records"
        )
        supervisor.msg_store.append_event(
            scope="maintenance",
            sender="maintenance",
            subject="prune",
            payload={
                "message": activity_summary(
                    summary=(
                        f"Pruned {events_count} {events_word}, "
                        f"{heartbeats_count} {heartbeats_word}"
                    ),
                    severity="routine",
                    verb="pruned",
                    subject="maintenance",
                    events=events_count,
                    heartbeats=heartbeats_count,
                ),
                "events": events_count,
                "heartbeats": heartbeats_count,
            },
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
