**Last Verified:** 2026-04-22

## Summary

The durable job queue is the substrate the heartbeat uses to execute recurring handlers. Jobs live in the `work_jobs` table on the per-project state DB; the `JobWorkerPool` drains them against the plugin host's `JobHandlerRegistry`. Claim semantics are atomic (SQLite `UPDATE ... RETURNING`), dedupe is enforced with a unique-when-pending partial index on `dedupe_key`, and retries use exponential backoff with an `attempt` / `max_attempts` cap per registration.

Touch this module when changing claim semantics, retry policy, or the `work_jobs` schema. Do not schedule work directly with `JobQueue.enqueue` from application code — register a roster entry via `RosterAPI.register_recurring` or enqueue from a subscribed hook.

## Core Contracts

```python
# src/pollypm/jobs/queue.py
JobId = int

class JobStatus(str, Enum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    DONE = "done"
    FAILED = "failed"

@dataclass(slots=True)
class Job: ...

@dataclass(slots=True)
class QueueStats:
    def total(self) -> int: ...

# RetryPolicy is a Callable[[int], timedelta] — given an attempt count,
# return the delay. `exponential_backoff(...)` is the factory.
RetryPolicy = Callable[[int], timedelta]

def exponential_backoff(
    base: timedelta, *, multiplier: float = 2.0, cap: timedelta | None = None,
) -> RetryPolicy: ...

class JobQueue:
    def __init__(self, state_db: Path) -> None: ...
    def enqueue(
        self, handler_name: str, payload: dict,
        *, dedupe_key: str | None = None, run_after: datetime | None = None,
    ) -> JobId | None: ...
    def claim(self, worker_id: str, *, limit: int = 1) -> list[Job]: ...
    def complete(self, job_id: JobId) -> None: ...
    def fail(self, job_id: JobId, error: str, *, retry: bool) -> None: ...
    def get(self, job_id: JobId) -> Job | None: ...
    def get_last_error(self, job_id: JobId) -> str | None: ...

# src/pollypm/jobs/workers.py
class JobWorkerPool:
    def __init__(self, queue: JobQueue, registry: JobHandlerRegistry,
                 *, concurrency: int = 4, poll_interval: float = 0.5) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
```

`pm jobs` CLI (via `src/pollypm/jobs/cli.py`):

```
pm jobs list [--status ...]
pm jobs stats
pm jobs drain                   # run remaining claimed/queued to completion
```

## File Structure

- `src/pollypm/jobs/__init__.py` — re-exports.
- `src/pollypm/jobs/queue.py` — `JobQueue`, `Job`, `JobStatus`, retry helpers.
- `src/pollypm/jobs/workers.py` — `JobWorkerPool`.
- `src/pollypm/jobs/registry.py` — `JobHandlerRegistry` owned by the plugin host.
- `src/pollypm/jobs/cli.py` — `pm jobs` commands.
- `src/pollypm/storage/state.py` — `work_jobs` table migration (schema version 6).

## Implementation Details

- **Claim atomicity.** `UPDATE ... RETURNING` (SQLite >= 3.35) guarantees two concurrent `claim()` calls cannot return the same row. The claim also stamps `claimed_by` and `claimed_at` so orphaned claims can be reaped.
- **Retry API.** `fail(job_id, error, retry=True)` requeues with the attempt counter incremented. `fail(..., retry=False)` moves the job straight to `failed`. `complete(job_id)` moves a claimed job to `done`.
- **Dedupe.** `CREATE UNIQUE INDEX idx_work_jobs_dedupe_queued ON work_jobs(dedupe_key) WHERE dedupe_key IS NOT NULL AND status IN ('queued','claimed')`. Enqueue returns `None` when a matching pending job exists.
- **Retries.** Handler registrations specify `max_attempts` and `timeout_seconds`. Failed jobs below `max_attempts` reschedule with `exponential_backoff(attempt, base=5.0, cap=300.0)` applied to `run_after`. Failed jobs at `max_attempts` land in `failed` with `last_error`.
- **Handler timeout.** Enforced by `JobWorkerPool` via a thread-level wall clock — the handler callable runs synchronously, but a watchdog cancels the claim if it exceeds the registered timeout. Handlers that need longer budgets (e.g. `transcript.ingest` at 600s) raise the timeout on registration.
- **Concurrency.** `[heartbeat.workers].concurrency` in `pollypm.toml` sets the pool size (default 4). Workers are daemon threads.

## Related Docs

- [modules/heartbeat.md](../modules/heartbeat.md) — enqueues from roster ticks.
- [plugins/core-recurring.md](../plugins/core-recurring.md) — canonical handler registrations.
- [modules/state-store.md](../modules/state-store.md) — `work_jobs` schema migration.
