**Last Verified:** 2026-04-22

## Summary

`task_assignment_notify` subscribes to the in-process `TaskAssignmentEvent` bus (fed by `SQLiteWorkService._sync_transition`) and delivers worker / reviewer / architect pings with a 30-minute dedupe window. A `@every 30s` sweeper catches events dropped on restart or emitted before the target session existed. A `SessionCreatedEvent` listener replays outstanding queued / in_progress / review pings the moment a fresh worker session comes online so a restarted worker gets its resume ping without waiting for the sweeper.

No SessionService protocol change — the target session is resolved purely by naming convention (`worker-<project>` / `worker_<project>`, `pm-reviewer`, etc.). See `resolver.notify` and issue #244 / #246.

Touch this plugin when changing the dedupe window, the replay statuses, or the session-name resolution rules.

## Core Contracts

Registered:

- Listener on `pollypm.work.task_assignment.TaskAssignmentEvent` bus.
- Listener on `pollypm.session_services.base.SessionCreatedEvent` bus.
- `task_assignment.notify` handler (triggered directly by the bus, not via roster).
- `task_assignment.sweep` handler on a `@every 30s` roster entry.

Key constants:

- `DEDUPE_WINDOW_SECONDS = 1800` (30 minutes) — mirrors `DefaultRecoveryPolicy.STUCK_ON_TASK_SECONDS` so a `stuck_on_task` classification never out-races a resume ping.
- `_REPLAY_STATUSES = ("in_progress", "queued", "review")` — statuses replayed on `SessionCreatedEvent`.

## File Structure

- `src/pollypm/plugins_builtin/task_assignment_notify/plugin.py` — bus listeners + sweep roster.
- `src/pollypm/plugins_builtin/task_assignment_notify/pollypm-plugin.toml` — manifest.
- `src/pollypm/plugins_builtin/task_assignment_notify/resolver.py` — `notify(event, ...)`, `load_runtime_services`, session-name conventions.
- `src/pollypm/plugins_builtin/task_assignment_notify/handlers/notify.py` — `task_assignment_notify_handler`, `event_to_payload`.
- `src/pollypm/plugins_builtin/task_assignment_notify/handlers/sweep.py` — `task_assignment_sweep_handler`, `_build_event_for_task`.
- `src/pollypm/work/task_assignment.py` — the event bus (`TaskAssignmentEvent`, `subscribe`, `dispatch`).

## Implementation Details

- **Session resolution.** `resolver.notify` maps an event to a session name by convention. Worker events go to `worker-<project>` or `worker_<project>` (whichever exists); review events go to `pm-reviewer`. A missing session causes the event to be requeued for the sweep.
- **Dedupe.** A keyed hash of `(task_id, recipient, kind)` is recorded with timestamp. Events within 30 minutes of a previous identical event are skipped. Dedupe state lives in the state DB so restarts preserve the window.
- **Replay on session birth.** `_REPLAY_STATUSES` captures the three lifecycle states where a restarted worker should immediately receive a resume ping instead of waiting up to 30 seconds for the sweep.
- **Sweeper.** Walks `in_progress` / `queued` / `review` tasks whose assigned session is live and whose last ping is older than the dedupe window. Rebuilds a synthetic `TaskAssignmentEvent` via `_build_event_for_task` and runs it through the same resolver.

## Related Docs

- [modules/work-service.md](../modules/work-service.md) — bus producer.
- [modules/session-services.md](../modules/session-services.md) — `SessionCreatedEvent` bus.
- [plugins/human-notify.md](human-notify.md) — parallel human-targeted fanout.
- [modules/recovery.md](../modules/recovery.md) — `STUCK_ON_TASK_SECONDS` alignment.
