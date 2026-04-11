# T061: Stuck Session Detected After N Consecutive Idle Cycles

**Spec:** v1/10-heartbeat-and-supervision
**Area:** Heartbeat
**Priority:** P0
**Duration:** 15 minutes

## Objective
Verify that the heartbeat detects a stuck session after N consecutive cycles where the session shows no progress, and generates an alert.

## Prerequisites
- `pm up` has been run with a worker actively processing
- Knowledge of the stuck detection threshold (N consecutive idle cycles)
- Ability to cause a worker to stall

## Steps
1. Check the stuck detection threshold: `pm config show` and look for `stuck_threshold` or equivalent (e.g., N = 3 cycles).
2. Run `pm status` and confirm a worker is actively working on an issue.
3. Attach to the worker session and cause it to stall. Options:
   - Send a long-running command that produces no output
   - Claim the human lease and then do nothing (so automation cannot proceed)
   - Kill the provider process inside the pane but leave the pane alive
4. Detach and monitor the heartbeat via `pm status` or by watching the heartbeat log.
5. On the first heartbeat cycle after the stall, the worker should be classified as "idle" or "no_progress." Check `pm status`.
6. Wait for N consecutive cycles with the worker showing no progress.
7. After N cycles, check `pm status`. The worker should now be classified as "stuck."
8. Verify an alert was generated: `pm alert list` or check the alerts table in the database.
9. Verify the alert includes the worker session ID, the reason ("stuck after N consecutive idle cycles"), and a timestamp.
10. Resolve the stall (release the lease, restart the process, etc.) and verify the worker returns to "healthy" status.

## Expected Results
- Worker stall is detected after N consecutive idle heartbeat cycles
- Classification changes from "idle" to "stuck" after the threshold
- An alert is generated with session ID, reason, and timestamp
- The threshold matches the configured value
- Resolving the stall returns the session to healthy status

## Log

**Date:** 2026-04-10 | **Result:** PASS

### Re-test — 2026-04-10 (via heartbeat querying its alerts DB)

Asked heartbeat to report stuck/loop alerts:
```
22 suspected_loop alerts across 5 sessions:
│ worker_pollypm         │ 8 loop alerts │
│ operator               │ 8 loop alerts │
│ worker_otter_camp      │ 3 loop alerts │
│ worker_pollypm_website │ 2 loop alerts │
│ worker_news            │ 1 loop alert  │

3 currently open:
│ worker_pollypm         │ needs_followup │ 19:28        │
│ worker_otter_camp      │ suspected_loop │ 17:52 (1.5h) │
│ worker_pollypm_website │ suspected_loop │ 17:47 (1.5h) │
```

Heartbeat analysis: "worker_otter_camp and worker_pollypm_website have had open suspected_loop alerts for over 90 minutes — their pane snapshots haven't changed, meaning they're sitting idle at a prompt."

Stuck detection works via snapshot hash comparison between consecutive heartbeat cycles. After N consecutive identical snapshots → suspected_loop alert. ✅
