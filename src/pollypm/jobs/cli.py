"""Operator CLI for the durable job queue (``pm jobs ...``).

Thin wrappers over ``JobQueue`` public API. Covers the operator-visible
surface for issue #165:

    pm jobs list [--status=...] [--handler=NAME] [--limit=N] [--json]
    pm jobs show <job_id> [--json]
    pm jobs retry <job_id>
    pm jobs purge --status=failed
    pm jobs drain
    pm jobs stats [--json]

The commands take an optional ``--config`` pointing at a ``pollypm.toml``;
the queue opens on the project's ``state.db`` so that entries enqueued by
the running supervisor show up here.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import typer

from pollypm.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path
from pollypm.jobs import Job, JobQueue, JobStatus


__all__ = ["jobs_app", "build_queue_for_config"]


jobs_app = typer.Typer(
    help=(
        "Inspect and manage the durable job queue.\n\n"
        "Examples:\n\n"
        "• pm jobs list                       — show queued / running jobs\n"
        "• pm jobs show <id>                  — inspect one job\n"
        "• pm jobs retry <id>                 — retry a failed job\n"
    )
)


# ---------------------------------------------------------------------------
# Queue wiring
# ---------------------------------------------------------------------------


_QueueFactory = Callable[[Path], JobQueue]

# Hook to override queue construction in tests — set to a callable taking a
# config path and returning a ``JobQueue``. When ``None`` the default factory
# (``build_queue_for_config``) is used.
_queue_factory: _QueueFactory | None = None


def set_queue_factory(factory: _QueueFactory | None) -> None:
    """Install a queue factory (used by tests). Pass ``None`` to reset."""
    global _queue_factory
    _queue_factory = factory


def build_queue_for_config(config_path: Path) -> JobQueue:
    """Default factory: open a ``JobQueue`` against the project's state DB."""
    resolved = resolve_config_path(config_path)
    if not resolved.exists():
        from pollypm.errors import format_config_not_found_error

        raise typer.BadParameter(format_config_not_found_error(resolved))
    config = load_config(resolved)
    db_path = config.project.state_db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return JobQueue(db_path=db_path)


def _open_queue(config_path: Path) -> JobQueue:
    if _queue_factory is not None:
        return _queue_factory(config_path)
    return build_queue_for_config(config_path)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _parse_status(status: str | None) -> JobStatus | None:
    if status is None:
        return None
    try:
        return JobStatus(status)
    except ValueError as exc:
        raise typer.BadParameter(
            f"Unknown status {status!r}. Valid: queued, claimed, done, failed.",
        ) from exc


def _fmt_ts(dt: datetime | None) -> str:
    if dt is None:
        return "-"
    # Compact ISO without microseconds; preserves tz when present.
    return dt.replace(microsecond=0).isoformat()


def _job_to_dict(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "handler": job.handler_name,
        "status": job.status.value,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "dedupe_key": job.dedupe_key,
        "payload": job.payload,
        "enqueued_at": _fmt_ts(job.enqueued_at),
        "run_after": _fmt_ts(job.run_after),
        "claimed_at": _fmt_ts(job.claimed_at),
        "claimed_by": job.claimed_by,
    }


def _emit_json(payload: object) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _print_job_row(job: Job) -> None:
    typer.echo(
        f"{job.id:>6}  {job.status.value:<8}  "
        f"attempt={job.attempt}/{job.max_attempts}  "
        f"handler={job.handler_name}  "
        f"run_after={_fmt_ts(job.run_after)}"
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@jobs_app.command("list")
def jobs_list(
    status: str | None = typer.Option(
        None, "--status", help="Filter by status: queued, claimed, done, failed.",
    ),
    handler: str | None = typer.Option(
        None, "--handler", help="Filter by handler name (exact match).",
    ),
    limit: int = typer.Option(50, "--limit", help="Maximum rows to return."),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
) -> None:
    """List jobs in the queue, most recent first."""
    parsed_status = _parse_status(status)
    if limit <= 0:
        raise typer.BadParameter("--limit must be >= 1")

    queue = _open_queue(config_path)
    try:
        # Over-fetch when filtering by handler so --limit still returns up to N
        # matching rows rather than N scanned rows.
        fetch_limit = limit if handler is None else max(limit * 5, limit)
        rows = queue.list_jobs(status=parsed_status, limit=fetch_limit)
        if handler is not None:
            rows = [row for row in rows if row.handler_name == handler]
        rows = rows[:limit]
    finally:
        queue.close()

    if json_output:
        _emit_json([_job_to_dict(row) for row in rows])
        return

    if not rows:
        typer.echo("No jobs match.")
        return

    for row in rows:
        _print_job_row(row)


@jobs_app.command("show")
def jobs_show(
    job_id: int = typer.Argument(..., help="Job id."),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
) -> None:
    """Show full details for a single job."""
    queue = _open_queue(config_path)
    try:
        job = queue.get(job_id)
        last_error = queue.get_last_error(job_id) if job is not None else None
    finally:
        queue.close()

    if job is None:
        typer.echo(f"Job {job_id} not found.", err=True)
        raise typer.Exit(code=1)

    payload = _job_to_dict(job)
    payload["last_error"] = last_error

    if json_output:
        _emit_json(payload)
        return

    typer.echo(f"id            = {payload['id']}")
    typer.echo(f"handler       = {payload['handler']}")
    typer.echo(f"status        = {payload['status']}")
    typer.echo(f"attempt       = {payload['attempt']}/{payload['max_attempts']}")
    typer.echo(f"dedupe_key    = {payload['dedupe_key'] or '-'}")
    typer.echo(f"enqueued_at   = {payload['enqueued_at']}")
    typer.echo(f"run_after     = {payload['run_after']}")
    typer.echo(f"claimed_at    = {payload['claimed_at']}")
    typer.echo(f"claimed_by    = {payload['claimed_by'] or '-'}")
    typer.echo(f"payload       = {json.dumps(payload['payload'], sort_keys=True)}")
    if last_error:
        typer.echo("last_error    =")
        for line in str(last_error).splitlines() or [""]:
            typer.echo(f"  {line}")


@jobs_app.command("retry")
def jobs_retry(
    job_id: int = typer.Argument(..., help="Job id."),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
) -> None:
    """Re-queue a failed job.

    Resets the attempt count to 0 and clears the last error so the handler
    starts fresh. The job's original max_attempts and payload are preserved.
    The operation is only allowed on jobs in the ``failed`` terminal state —
    use ``pm jobs list --status=failed`` to find candidates.
    """
    queue = _open_queue(config_path)
    try:
        job = queue.get(job_id)
        if job is None:
            typer.echo(f"Job {job_id} not found.", err=True)
            raise typer.Exit(code=1)
        if job.status is not JobStatus.FAILED:
            typer.echo(
                f"Job {job_id} is {job.status.value}, not failed — refusing to retry.",
                err=True,
            )
            raise typer.Exit(code=1)
        # Retry resets attempt count back to zero; document in --help above.
        with queue._lock:  # noqa: SLF001 — thin wrapper; reuses queue's own lock
            queue._conn.execute(
                """
                UPDATE work_jobs
                SET status = 'queued',
                    attempt = 0,
                    claimed_at = NULL,
                    claimed_by = NULL,
                    finished_at = NULL,
                    last_error = NULL,
                    run_after = ?
                WHERE id = ?
                """,
                (datetime.now().astimezone().isoformat(), int(job_id)),
            )
        typer.echo(f"Re-queued job {job_id} (attempt reset to 0).")
    finally:
        queue.close()


@jobs_app.command("purge")
def jobs_purge(
    status: str = typer.Option(
        ..., "--status", help="Terminal status to purge: done or failed.",
    ),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
) -> None:
    """Bulk delete terminal jobs. Only ``done`` and ``failed`` are accepted."""
    parsed = _parse_status(status)
    if parsed not in (JobStatus.DONE, JobStatus.FAILED):
        raise typer.BadParameter(
            "--status must be 'done' or 'failed' for purge (terminal states only).",
        )
    queue = _open_queue(config_path)
    try:
        with queue._lock:  # noqa: SLF001
            cursor = queue._conn.execute(
                "DELETE FROM work_jobs WHERE status = ?",
                (parsed.value,),
            )
            deleted = cursor.rowcount or 0
    finally:
        queue.close()
    typer.echo(f"Purged {deleted} {parsed.value} job(s).")


@jobs_app.command("drain")
def jobs_drain(
    timeout: float = typer.Option(
        30.0, "--timeout", help="Maximum seconds to wait for the queue to empty.",
    ),
    poll_interval: float = typer.Option(
        0.5, "--poll-interval", help="How often to re-check queue state (seconds).",
    ),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
) -> None:
    """Wait for queued + claimed jobs to drain to zero.

    Does not stop workers or refuse new enqueues — that lives in the running
    supervisor. This is a convenience for operators running a shutdown: call
    it after stopping the producer, wait for in-flight work to finish.
    """
    if timeout < 0:
        raise typer.BadParameter("--timeout must be non-negative")
    if poll_interval <= 0:
        raise typer.BadParameter("--poll-interval must be positive")

    queue = _open_queue(config_path)
    try:
        deadline = time.monotonic() + timeout
        while True:
            stats = queue.stats()
            pending = stats.queued + stats.claimed
            if pending == 0:
                typer.echo("Queue drained.")
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                typer.echo(
                    f"Timed out waiting for drain: queued={stats.queued} "
                    f"claimed={stats.claimed}",
                    err=True,
                )
                raise typer.Exit(code=1)
            time.sleep(min(poll_interval, remaining))
    finally:
        queue.close()


@jobs_app.command("stats")
def jobs_stats(
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
) -> None:
    """Show counts by status and top handlers by volume."""
    queue = _open_queue(config_path)
    try:
        stats = queue.stats()
        with queue._lock:  # noqa: SLF001
            rows = queue._conn.execute(
                """
                SELECT handler_name, COUNT(*)
                FROM work_jobs
                GROUP BY handler_name
                ORDER BY COUNT(*) DESC
                LIMIT 10
                """
            ).fetchall()
    finally:
        queue.close()

    per_handler = [{"handler": row[0], "count": int(row[1])} for row in rows]
    payload = {
        "queued": stats.queued,
        "claimed": stats.claimed,
        "done": stats.done,
        "failed": stats.failed,
        "total": stats.total,
        "top_handlers": per_handler,
    }

    if json_output:
        _emit_json(payload)
        return

    typer.echo(f"queued  = {stats.queued}")
    typer.echo(f"claimed = {stats.claimed}")
    typer.echo(f"done    = {stats.done}")
    typer.echo(f"failed  = {stats.failed}")
    typer.echo(f"total   = {stats.total}")
    if per_handler:
        typer.echo("")
        typer.echo("top handlers:")
        for item in per_handler:
            typer.echo(f"  {item['count']:>6}  {item['handler']}")
