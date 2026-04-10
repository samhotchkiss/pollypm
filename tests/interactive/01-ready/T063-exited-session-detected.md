# T063: Exited Session Detected When Process Gone

**Spec:** v1/10-heartbeat-and-supervision
**Area:** Heartbeat
**Priority:** P0
**Duration:** 10 minutes

## Objective
Verify that the heartbeat promptly detects when a session's process has exited and classifies it accordingly.

## Prerequisites
- `pm up` has been run with all sessions active
- Ability to kill a session process

## Steps
1. Run `pm status` and confirm all sessions are running. Note the PID of a worker process.
2. Attach to the heartbeat session briefly and note the current cycle number.
3. Kill the worker process: `kill -9 <worker-PID>`.
4. Wait for the next heartbeat cycle (check the heartbeat interval from config).
5. Run `pm status` and verify the killed worker is classified as "exited."
6. Verify the detection happened within one heartbeat cycle of the kill.
7. Check for an alert: `pm alert list` should show an "exited" alert for the worker session.
8. Verify the alert includes:
   - Session ID
   - Classification: "exited"
   - Timestamp of detection
   - Previous status (e.g., was "healthy" before exit)
9. Observe the auto-recovery process: the system should relaunch the worker.
10. After relaunch, verify `pm status` shows the worker as "healthy" again.

## Expected Results
- Exited session is detected on the next heartbeat cycle
- Classification changes to "exited" promptly
- An alert is generated with relevant details
- Auto-recovery relaunches the session
- Session returns to healthy status after recovery
- The entire detection-to-recovery process is logged

## Log
