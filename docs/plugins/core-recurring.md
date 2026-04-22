**Last Verified:** 2026-04-22

## Summary

`core_recurring` is the built-in plugin that registers the recurring handlers migrated off the old heartbeat loop. It wires ~15 job handlers (session health, capacity probe, account usage refresh, transcript ingest, alerts GC, work progress sweep, pane classification, DB vacuum, memory TTL sweep, events retention sweep, notification staging prune, agent-worktree prune, log rotate, worktree state audit) and a matching set of roster entries at their canonical cadences.

Touch this plugin when adding a new recurring handler, retiring one, or changing cadence. Do not implement handlers here — put them in sibling modules (`maintenance.py`, `sweeps.py`, `shared.py`) and register the wiring here.

## Core Contracts

Handler registrations (from `_register_handlers`):

| Handler | Max attempts | Timeout (s) | Source |
|---|---|---|---|
| `session.health_sweep` | 1 | 120 | `plugin.py:session_health_sweep_handler` |
| `capacity.probe` | 2 | 30 | `maintenance.py:capacity_probe_handler` |
| `account.usage_refresh` | 1 | 300 | `maintenance.py:account_usage_refresh_handler` |
| `transcript.ingest` | 2 | 600 | `maintenance.py:transcript_ingest_handler` |
| `alerts.gc` | 1 | 60 | `plugin.py:alerts_gc_handler` |
| `work.progress_sweep` | 1 | 60 | `sweeps.py:work_progress_sweep_handler` |
| `pane.classify` | 1 | 60 | `sweeps.py:pane_text_classify_handler` |
| `db.vacuum` | 1 | 120 | `maintenance.py:db_vacuum_handler` |
| `memory.ttl_sweep` | 1 | 60 | `maintenance.py:memory_ttl_sweep_handler` |
| `events.retention_sweep` | 1 | 60 | `plugin.py:events_retention_sweep_handler` |
| `notification_staging.prune` | 1 | 60 | `maintenance.py:notification_staging_prune_handler` |
| `agent_worktree.prune` | 1 | 120 | `maintenance.py:agent_worktree_prune_handler` |
| `log.rotate` | 1 | 120 | `maintenance.py:log_rotate_handler` |
| `worktree.state_audit` | 1 | 120 | `sweeps.py:worktree_state_audit_handler` |

Roster schedule (from `_register_roster`):

```
@every 10s  session.health_sweep
@every 60s  capacity.probe
@every 5m   account.usage_refresh
@every 5m   transcript.ingest
@every 5m   alerts.gc
@every 5m   work.progress_sweep
@every 30s  pane.classify
7 4 * * *   db.vacuum
13 4 * * *  memory.ttl_sweep
37 * * * *  events.retention_sweep
19 4 * * *  notification_staging.prune
23 * * * *  agent_worktree.prune
31 * * * *  log.rotate
@every 10m  worktree.state_audit
```

## File Structure

- `src/pollypm/plugins_builtin/core_recurring/plugin.py` — registration + `session.health_sweep`, `alerts.gc`, `events.retention_sweep` handlers.
- `src/pollypm/plugins_builtin/core_recurring/pollypm-plugin.toml` — manifest.
- `src/pollypm/plugins_builtin/core_recurring/maintenance.py` — most handler bodies + event-subject tier constants (`AUDIT_EVENT_SUBJECTS`, `OPERATIONAL_EVENT_SUBJECTS`, `HIGH_VOLUME_EVENT_SUBJECTS`).
- `src/pollypm/plugins_builtin/core_recurring/sweeps.py` — pane classify + work progress + worktree audit handlers.
- `src/pollypm/plugins_builtin/core_recurring/shared.py` — shared helpers (`_load_config_and_store`, ephemeral-session helpers, message-store open/close).

## Implementation Details

- **`session.health_sweep`** constructs a fresh `Supervisor` per tick and calls `run_heartbeat(snapshot_lines=200)`. Also runs the ephemeral-session sweeper to raise alerts for planned-exit panes that never cleaned up. This is the primary recovery trigger.
- **`events.retention_sweep`** applies tiered retention to `type='event'` rows in the unified `messages` table: audit / operational / high-volume / default tiers, each with its own `[events]` retention setting. Pinned events (`payload_json -> "$.pinned" = 1`) are never pruned.
- **Why `events.retention_sweep` runs hourly at `:37`.** Spreads the big cleanup away from db.vacuum (daily 04:07), memory.ttl_sweep (daily 04:13), notification_staging.prune (daily 04:19), and agent_worktree.prune (hourly at `:23`). Staggering avoids "everything wakes up at minute 0" contention on the write pool.
- **`transcript.ingest` 600s timeout.** First-cold-scan on a freshly installed PollyPM can legitimately take many minutes — bumping from 30s to 10m after the 2026-04-19 incident (see commit `e14f800`).
- **Handler vs. schedule separation.** Handlers are registered via `register_handlers` (callback), roster via `register_roster` (callback). The plugin host invokes both once at bootstrap. A cadence change in config requires a rail restart.

## Related Docs

- [modules/heartbeat.md](../modules/heartbeat.md) — the rail that runs the roster.
- [features/jobs-queue.md](../features/jobs-queue.md) — where the enqueued jobs land.
- [modules/state-store.md](../modules/state-store.md) — `events` + `messages` retention.
