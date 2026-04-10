# T082: Event Log Records All Lifecycle Events

**Spec:** v1/13-security-and-observability
**Area:** Observability
**Priority:** P1
**Duration:** 10 minutes

## Objective
Verify that the event log captures all major lifecycle events: session starts, stops, health checks, issue assignments, checkpoints, alerts, and interventions.

## Prerequisites
- `pm up` has been run and sessions have been active for several minutes
- Some activity has occurred (issues created, sessions cycled)

## Steps
1. Access the event log: `pm log` or `pm event list` (or query the database directly).
2. Verify the log contains **session start events** for all sessions launched during `pm up`. Look for events with type "session.started" and timestamps matching startup time.
3. Verify **heartbeat cycle events** are present: periodic entries with type "heartbeat.cycle" or similar.
4. Verify **issue lifecycle events**: if issues were created and transitioned, look for "issue.created", "issue.transitioned", "issue.assigned" events.
5. Verify **checkpoint events**: look for "checkpoint.created" events at various levels.
6. If any alerts were generated, verify **alert events** are present.
7. If any interventions occurred (nudge, reset, relaunch), verify **intervention events** are logged.
8. Run `pm down` to trigger session stop events.
9. Check the log for **session stop events** with type "session.stopped" or similar, with timestamps matching the shutdown.
10. Verify each event includes: event type, timestamp, session/entity ID, and relevant metadata.

## Expected Results
- All major lifecycle event types are present in the log
- Events include type, timestamp, entity ID, and metadata
- Events are in chronological order
- Session start and stop events bracket the session's lifetime
- Heartbeat cycles create regular event entries
- Issue and checkpoint events are captured as they occur

## Log
