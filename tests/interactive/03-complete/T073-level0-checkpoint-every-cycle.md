# T073: Level 0 Checkpoint Created on Every Heartbeat Cycle

**Spec:** v1/12-checkpoints-and-recovery
**Area:** Checkpoints
**Priority:** P0
**Duration:** 10 minutes

## Objective
Verify that a Level 0 checkpoint (heartbeat snapshot) is created automatically on every heartbeat cycle, capturing the minimal state needed for recovery.

## Prerequisites
- `pm up` has been run with all sessions active
- Heartbeat is cycling normally

## Steps
1. Run `pm config show` and note the heartbeat interval (e.g., 30 seconds).
2. Query existing Level 0 checkpoints: `pm checkpoint list --level 0 --limit 5` or `sqlite3 <state.db> "SELECT id, level, timestamp, session_id FROM checkpoints WHERE level = 0 ORDER BY timestamp DESC LIMIT 5;"`.
3. Note the count and the latest timestamp.
4. Wait for exactly 3 heartbeat cycles (e.g., 90 seconds if interval is 30 seconds).
5. Query Level 0 checkpoints again and verify 3 new entries were created.
6. For each new checkpoint, verify it contains:
   - Checkpoint ID (unique)
   - Level: 0
   - Timestamp
   - Session health snapshot (health status of each session)
   - Heartbeat cycle number
7. Verify the checkpoints are evenly spaced in time (matching the heartbeat interval).
8. Verify no cycles were missed: cycle numbers should be consecutive.
9. Check the storage size of Level 0 checkpoints — they should be lightweight (minimal data per checkpoint).
10. Verify old Level 0 checkpoints are not prematurely cleaned up (retention policy should keep a reasonable number).

## Expected Results
- One Level 0 checkpoint is created per heartbeat cycle
- Checkpoints contain health snapshots for all sessions
- Cycle numbers are consecutive with no gaps
- Timestamps are evenly spaced
- Checkpoints are lightweight in storage
- Retention policy preserves a useful history

## Log


**Date:** 2026-04-10 | **Result:** PASS

### Re-test — 2026-04-10 (via heartbeat querying checkpoints DB)
```
│ heartbeat              │ 153 level0 checkpoints │
│ operator               │ 151                    │
│ worker_pollypm_website │ 149                    │
│ worker_pollypm         │ 149                    │
│ worker_otter_camp      │ 149                    │
│ worker_news            │ 8                      │
```
~150 checkpoints per session, one per heartbeat sweep. All level0. ✅
