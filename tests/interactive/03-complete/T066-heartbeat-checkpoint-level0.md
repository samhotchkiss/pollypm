# T066: Heartbeat Checkpoints (Level 0) Recorded Every Cycle

**Spec:** v1/10-heartbeat-and-supervision
**Area:** Heartbeat
**Priority:** P1
**Duration:** 10 minutes

## Objective
Verify that a Level 0 checkpoint is recorded on every heartbeat cycle, capturing the state of all sessions at that point in time.

## Prerequisites
- `pm up` has been run with all sessions active
- Heartbeat is running and cycling

## Steps
1. Run `pm status` and note the heartbeat cycle interval.
2. Check the checkpoint store for existing Level 0 checkpoints. Look in the database: `sqlite3 <state.db> "SELECT * FROM checkpoints WHERE level = 0 ORDER BY timestamp DESC LIMIT 5;"` (adjust table/column names).
3. Note the latest checkpoint timestamp and cycle number.
4. Wait for 3 heartbeat cycles (e.g., if interval is 30 seconds, wait ~90 seconds).
5. Query the checkpoints again: `sqlite3 <state.db> "SELECT * FROM checkpoints WHERE level = 0 ORDER BY timestamp DESC LIMIT 10;"`.
6. Verify that 3 new Level 0 checkpoints were created (one per cycle).
7. Verify each checkpoint contains:
   - Checkpoint ID
   - Level: 0
   - Timestamp
   - Cycle number
   - Health classifications for all sessions at that moment
8. Verify the timestamps are spaced by approximately the heartbeat interval.
9. Verify the cycle numbers are consecutive (no gaps).
10. Run `pm checkpoint list --level 0 --limit 10` (or equivalent) and verify the CLI output matches the database records.

## Expected Results
- One Level 0 checkpoint is created per heartbeat cycle
- Checkpoints contain all required fields (ID, level, timestamp, cycle, health data)
- Timestamps are consistent with the heartbeat interval
- Cycle numbers are consecutive
- Checkpoints are queryable via both CLI and database
- No checkpoints are missed or duplicated

## Log

**Date:** 2026-04-10 | **Result:** PASS

71 level0 checkpoints for heartbeat session. Verified in T073. One checkpoint per heartbeat cycle.

### Re-test — 2026-04-10 (heartbeat self-reported)

759 level0 checkpoints across 6 sessions (149-153 each). One per heartbeat sweep. ✅

### Deep test — 2026-04-10 (heartbeat showed checkpoint breakdown)

759 level0 checkpoints: heartbeat(153), operator(151), workers(149 each), worker_news(8). All level0 — one per sweep. ✅
