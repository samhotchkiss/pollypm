# T032: Health Classification Parsed Correctly from Pane Output

**Spec:** v1/05-provider-sdk
**Area:** Provider Adapters
**Priority:** P0
**Duration:** 10 minutes

## Objective
Verify that the heartbeat system correctly parses pane output to classify session health status (healthy, idle, stuck, looping, exited).

## Prerequisites
- `pm up` has been run and all sessions are active
- Heartbeat is running and performing health checks

## Steps
1. Run `pm status` and note the health classification for each session.
2. Attach to the heartbeat session and observe a health check cycle. Note the raw pane output it reads and the classification it assigns.
3. Verify a healthy, active worker shows as "healthy" or "working" — the classification should indicate the session is making progress.
4. If a worker is idle (no assigned issue), verify it shows as "idle" — not "stuck" or "unhealthy."
5. Simulate a stuck session: attach to a worker and cause it to hang (e.g., by entering a long-running command that produces no output for several cycles).
6. Wait for 2-3 heartbeat cycles and check `pm status`. The stuck worker should be classified as "stuck" or "unresponsive."
7. Simulate an exited session: kill the worker process (`kill -9 <PID>`).
8. On the next heartbeat cycle, check `pm status`. The exited worker should be classified as "exited."
9. Wait for auto-recovery and verify the classification returns to "healthy" after relaunch.
10. Verify all classifications in the event log match what `pm status` reported.

## Expected Results
- Active sessions are classified as "healthy" or "working"
- Idle sessions are classified as "idle"
- Stuck sessions are classified as "stuck" after the detection threshold
- Exited sessions are classified as "exited" promptly
- Classifications update on each heartbeat cycle
- Event log records each classification change

## Log
