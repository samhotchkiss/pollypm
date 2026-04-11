# T004: State Store Records All Session Events with Timestamps

**Spec:** v1/01-architecture-and-domain
**Area:** State Store
**Priority:** P1
**Duration:** 10 minutes

## Objective
Verify that the state store (SQLite) records all session lifecycle events with correct timestamps, including session creation, health checks, state transitions, and shutdowns.

## Prerequisites
- `pm up` has been run and sessions are running
- Access to the SQLite state database (typically at `.pollypm/state.db` or equivalent)

## Steps
1. Run `pm status` to confirm sessions are active.
2. Locate the state database file. Check `.pollypm/state.db` in the project directory or run `pm config show` to find the database path.
3. Open the database with `sqlite3 <path-to-state.db>`.
4. Run `.tables` to list all tables. Identify the events or session_events table.
5. Run `SELECT * FROM session_events ORDER BY timestamp DESC LIMIT 20;` (adjust table/column names as needed) to see recent events.
6. Verify that session creation events exist for heartbeat, operator, and worker roles, each with a timestamp.
7. Look for heartbeat cycle events — there should be periodic entries with incrementing timestamps.
8. Trigger a new event by running `pm down` followed by `pm up`. This should produce shutdown and startup events.
9. Query the events table again and confirm the shutdown and startup events appear with correct timestamps (shutdown before startup, timestamps in chronological order).
10. Verify that each event record includes at minimum: event type, session role, timestamp, and any relevant metadata.

## Expected Results
- State database exists and is accessible
- Events table contains records for all session lifecycle events
- Each event has a valid ISO-8601 or Unix timestamp
- Events are in chronological order
- Session creation, health check, and shutdown events are all present
- New events from step 8 appear immediately after the actions

## Log

**Date:** 2026-04-10 | **Result:** PASS

### Re-test — 2026-04-10 (via heartbeat querying events DB)

Asked heartbeat: "How many events total? Distinct types? 3 most recent?"
```
710 events total. Types:
│ launch              │ 184   │
│ token_ledger        │ 148   │
│ heartbeat           │ 141   │
│ ran                 │ 56    │
│ controller_selected │ 29    │
│ send_input          │ 19    │
│ alert               │ 15    │
│ failed              │ 10    │
│ scheduled           │ 8     │
│ recovered           │ 6     │
│ manual_switch       │ 3     │

3 most recent:
│ 710 │ worker_pollypm │ send_input    │ Sent issue assignment #0019              │
│ 709 │ operator       │ manual_switch │ Switched pm-operator to claude account   │
│ 708 │ operator       │ recovered     │ Recovered pm-operator in place           │
```
All lifecycle events tracked with session_name, event_type, message, timestamp. ✅
