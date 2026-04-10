# T060: Health Classified on Every Heartbeat Cycle

**Spec:** v1/10-heartbeat-and-supervision
**Area:** Heartbeat
**Priority:** P0
**Duration:** 10 minutes

## Objective
Verify that the heartbeat classifies the health of every session on every heartbeat cycle, producing a health record for each cycle.

## Prerequisites
- `pm up` has been run with all sessions active
- Heartbeat is running and performing cycles

## Steps
1. Run `pm status` and note the heartbeat cycle interval (or check `pm config show` for `heartbeat_interval`).
2. Attach to the heartbeat session via `pm console heartbeat` and observe the output.
3. Watch for at least 3 consecutive heartbeat cycles. Each cycle should produce output that includes health classifications for all sessions.
4. For each cycle, verify it includes:
   - Cycle number or timestamp
   - Health classification for the heartbeat itself (or skip-self)
   - Health classification for the operator session
   - Health classification for each worker session
5. Verify the classifications use the expected vocabulary (e.g., "healthy", "idle", "stuck", "looping", "exited").
6. Detach and check the state database for health records: `sqlite3 <state.db> "SELECT * FROM health_checks ORDER BY timestamp DESC LIMIT 20;"` (adjust table/column names).
7. Verify there is one health record per session per cycle.
8. Verify the time between cycles matches the configured interval (within a reasonable tolerance, e.g., +/- 5 seconds).
9. Run `pm status` and verify the displayed health matches the most recent heartbeat cycle's classifications.
10. Verify no cycles are skipped: the cycle numbers should be consecutive with no gaps.

## Expected Results
- Every heartbeat cycle produces health classifications for all sessions
- Classifications use the correct vocabulary
- Health records are stored in the database per-session per-cycle
- Cycle interval matches the configured heartbeat interval
- No cycles are skipped
- `pm status` reflects the latest classifications

## Log
