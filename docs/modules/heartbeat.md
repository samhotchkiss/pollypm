**Last Verified:** 2026-04-22

## Summary

The heartbeat is PollyPM's sealed recurring-work engine. It has three pieces, all small, all tested in isolation:

1. **`Heartbeat`** (`src/pollypm/heartbeat/tick.py`) — consults the `Roster` at a given `now`, enqueues due entries into a `JobQueue`, and updates `RosterEntry.last_fired_at`. That's the entire tick.
2. **`Roster`** (`src/pollypm/heartbeat/roster.py`) — immutable table of `RosterEntry` records keyed by `(handler_name, payload_hash)`. Entries have a `Schedule` (`OnStartupSchedule`, `EverySchedule`, `CronSchedule`) that decides `due_at(now)`.
3. **`JobQueue` + `JobWorkerPool`** (`src/pollypm/jobs/`) — durable SQLite-backed queue (`work_jobs` table) and a daemon worker pool that drains claimed jobs through the plugin-host's `JobHandlerRegistry`.

`HeartbeatRail` (`src/pollypm/heartbeat/boot.py`) is the wire-up object. It builds the roster from the plugin host, constructs the queue on `config.project.state_db`, starts the worker pool, and runs a daemon ticker thread calling `Heartbeat.tick()` every 15 seconds.

Touch this module when adding a new recurring handler (register via `RosterAPI.register_recurring` in your plugin), or when changing claim semantics / dedupe rules. Do not add cadence logic inside handlers — express it in the schedule string.

## Core Contracts

```python
# src/pollypm/heartbeat/tick.py
class JobQueueProtocol(Protocol):
    def enqueue(
        self,
        handler_name: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str | None = None,
        run_after: datetime | None = None,
    ) -> Any: ...

@dataclass(slots=True)
class EnqueuedJob: ...

@dataclass(slots=True)
class TickResult:
    fired: list[EnqueuedJob]
    skipped: list[str]        # handlers that had a due entry but were gated
    tick_at: datetime

class Heartbeat:
    roster: Roster
    queue: JobQueueProtocol
    last_tick_at: datetime | None
    def __init__(self, roster: Roster, queue: JobQueueProtocol) -> None: ...
    def tick(self, now: datetime | None = None) -> TickResult: ...

# src/pollypm/heartbeat/roster.py
class Schedule(Protocol):
    def next_due(self, after: datetime) -> datetime | None: ...
    def expression(self) -> str: ...

class OnStartupSchedule(Schedule): ...
class EverySchedule(Schedule): ...     # "@every 30s", "@every 5m", "@every 24h"
class CronSchedule(Schedule): ...      # 5-field cron expression

@dataclass(slots=True)
class RosterEntry:
    handler_name: str
    schedule: Schedule
    payload: dict[str, Any]
    dedupe_key: str | None
    last_fired_at: datetime | None

class Roster:
    def register_entry(self, entry: RosterEntry) -> bool: ...
    def register(self, *, handler_name, schedule, payload=None, dedupe_key=None) -> bool: ...
    def snapshot(self) -> list[RosterEntry]: ...
    def __iter__(self): ...
    def __len__(self) -> int: ...

def parse_schedule(expr: str) -> Schedule: ...

# src/pollypm/heartbeat/boot.py
@dataclass(slots=True)
class WorkerSettings:
    concurrency: int = 4
    poll_interval: float = 0.5

def load_worker_settings(config_path: Path) -> WorkerSettings: ...

class HeartbeatRail:
    roster: Roster
    queue: JobQueue
    pool: JobWorkerPool
    heartbeat: Heartbeat

    @classmethod
    def from_plugin_host(cls, *, state_db: Path, plugin_host) -> "HeartbeatRail": ...
    def start(self) -> None: ...
    def tick(self, now: datetime | None = None) -> TickResult: ...
    def stop(self) -> None: ...
```

## File Structure

- `src/pollypm/heartbeat/__init__.py` — re-exports `Heartbeat`, `Roster`, schedule classes.
- `src/pollypm/heartbeat/tick.py` — `Heartbeat`, `TickResult`, `EnqueuedJob`, `JobQueueProtocol`.
- `src/pollypm/heartbeat/roster.py` — `Roster`, `RosterEntry`, `Schedule` protocol, built-in schedule types, `parse_schedule`.
- `src/pollypm/heartbeat/boot.py` — `HeartbeatRail`, `WorkerSettings`, `load_worker_settings`.
- `src/pollypm/jobs/queue.py` — `JobQueue` (SQLite `work_jobs` table, atomic `UPDATE ... RETURNING` claims).
- `src/pollypm/jobs/workers.py` — `JobWorkerPool` draining the queue.
- `src/pollypm/jobs/registry.py` — the plugin-host-owned `JobHandlerRegistry`.
- `src/pollypm/plugins_builtin/core_recurring/plugin.py` — registers the built-in recurring handlers (session health sweep, capacity probe, account usage refresh, transcript ingest, etc.).

## Implementation Details

- **Overdue policy — at most one fire per tick.** If the ticker wakes late, each due entry is enqueued exactly once, not once per missed cadence. Rationale: most handlers are idempotent sweeps or already use `dedupe_key`; replaying N missed fires saturates the pool right when the system is stressed.
- **Dedupe.** `JobQueue` enforces a *unique-when-pending* index on `dedupe_key` restricted to `status IN ('queued','claimed')`. A second `enqueue(..., dedupe_key="foo")` while a `"foo"` job is still pending is a no-op.
- **Claiming.** Workers claim via SQLite `UPDATE ... RETURNING` (requires SQLite >= 3.35), which is atomic — two concurrent claims cannot return the same row.
- **Cadence knobs.** `DEFAULT_TICK_INTERVAL_SECONDS = 15.0` and `DEFAULT_WORKER_CONCURRENCY = 4`. `[heartbeat.workers]` in `pollypm.toml` can override `concurrency` and `poll_interval`.
- **Schedule strings.** `"@every 30s"`, `"@every 5m"`, `"@every 12h"`, and 5-field cron expressions (`"7 4 * * *"`) are supported. `parse_schedule` is the single entry point; plugins should never construct `Schedule` instances directly unless they need a custom type.
- **Payload snapshot.** The tick deep-copies mutable payloads before enqueueing so handlers cannot mutate the registry's record by holding references. Immutable scalar/tuple/frozenset payloads skip the copy (`_is_immutable_payload_value`).
- **Roster is rebuilt at rail start.** Plugins that register handlers or roster entries after `HeartbeatRail.start()` do *not* take effect until the next rail boot. This is deliberate — dynamic schedule changes belong in config files, not at runtime.

## Related Docs

- [modules/core-rail.md](core-rail.md) — owns the `HeartbeatRail` singleton.
- [modules/plugin-host.md](plugin-host.md) — `RosterAPI.register_recurring` + `JobHandlerAPI.register_handler`.
- [plugins/core-recurring.md](../plugins/core-recurring.md) — the canonical roster set.
- [features/jobs-queue.md](../features/jobs-queue.md) — the durable `work_jobs` queue.
