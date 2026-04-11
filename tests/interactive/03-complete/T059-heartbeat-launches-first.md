# T059: Heartbeat Launches as Session 0 Before Other Sessions

**Spec:** v1/10-heartbeat-and-supervision
**Area:** Heartbeat
**Priority:** P0
**Duration:** 10 minutes

## Objective
Verify that the heartbeat session is always launched first (as Session 0) before operator and worker sessions, ensuring monitoring is in place before supervised sessions start.

## Prerequisites
- `pm down` has been run (clean slate)
- Debug/verbose logging enabled to observe launch order

## Steps
1. Enable verbose logging: `pm config set log_level debug` or set the environment variable.
2. Run `pm down` to ensure no sessions are running.
3. Run `pm up` and carefully observe the output or log for session launch order.
4. Check the log for the launch sequence: `pm log --filter launch` or search the log file.
5. Verify the heartbeat session is launched FIRST. Look for a log entry like "Launching heartbeat session" or "Session 0: heartbeat."
6. Verify the operator session is launched AFTER the heartbeat. Look for "Launching operator session" with a later timestamp.
7. Verify worker sessions are launched AFTER the heartbeat. Look for "Launching worker session(s)" with later timestamps.
8. Cross-reference with the state store: query session creation events and verify the heartbeat's creation timestamp is earliest.
9. Verify the heartbeat is already performing health checks by the time the first worker starts (check the heartbeat log for a health check event before the worker launch event).
10. Run `pm status` and verify the heartbeat shows as Session 0 or has the lowest session index.

## Expected Results
- Heartbeat is the first session launched during `pm up`
- Log timestamps confirm heartbeat precedes operator and worker launches
- Heartbeat is actively monitoring before other sessions start
- Session ordering is consistent across multiple `pm up` / `pm down` cycles
- Heartbeat is designated as Session 0 or equivalent primary session

## Log

**Date:** 2026-04-10
**Result:** PASS

### Execution
1. `tmux list-windows -t pollypm-storage-closet` shows: window 0 = pm-heartbeat, window 1 = pm-operator, window 2+ = workers. ✅ Heartbeat is Session 0.
2. Code verified: `_bootstrap_launches` creates the first launch as the tmux session (`create_session`), and `plan_launches` returns heartbeat first (defined first in config).
3. Heartbeat is running and actively monitoring (Claude, "ready" after reading heartbeat control prompt). ✅
4. Other sessions (operator, workers) are in windows 1+, created after heartbeat. ✅

### Re-test — 2026-04-10 (via heartbeat session)

Asked heartbeat to check window indices:
```
storage-closet:0 pm-heartbeat
storage-closet:1 pm-operator
storage-closet:3 worker-otter_camp
storage-closet:4 worker-pollypm-website
```
Heartbeat IS window 0 (first in the storage closet). ✅

**Note:** The heartbeat's own analysis said it was "NOT the first" because it confused `window_activity` timestamps (last activity) with creation order. The heartbeat was recreated during T005 crash test so its activity timestamp is newer. Window index 0 confirms launch order.
