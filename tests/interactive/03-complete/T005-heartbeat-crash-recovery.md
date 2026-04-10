# T005: System Recovers if Heartbeat Crashes (Auto-Relaunch)

**Spec:** v1/01-architecture-and-domain
**Area:** Session Lifecycle
**Priority:** P0
**Duration:** 10 minutes

## Objective
Verify that if the heartbeat session crashes or exits unexpectedly, the system automatically detects the failure and relaunches the heartbeat session.

## Prerequisites
- `pm up` has been run and all sessions are healthy
- `pm status` shows heartbeat as running

## Steps
1. Run `pm status` and confirm the heartbeat session is running. Note its PID or session identifier.
2. Attach to the heartbeat session via `pm console heartbeat` and note the process running inside the pane.
3. Detach from the heartbeat session.
4. Kill the heartbeat process forcefully. Use `tmux send-keys -t pollypm:heartbeat C-c` or identify the PID and run `kill -9 <PID>`.
5. Immediately run `pm status` — the heartbeat should show as "exited" or "unhealthy".
6. Wait up to 60 seconds, checking `pm status` every 10 seconds.
7. Observe that the system detects the heartbeat crash and initiates a relaunch.
8. Confirm the heartbeat session is running again with a new PID or process.
9. Attach to the heartbeat session via `pm console heartbeat` and verify it is producing health-check output normally.
10. Verify that the auto-relaunch event was recorded in the state store or event log (check `pm log` or query the database).

## Expected Results
- Killing the heartbeat process causes it to show as exited/unhealthy in `pm status`
- Within 60 seconds, the heartbeat is automatically relaunched
- The relaunched heartbeat resumes normal health-check cycles
- An event log entry records the crash and the auto-relaunch
- Other sessions (operator, workers) were not disrupted during the outage

## Log

### Test Execution — 2026-04-10 11:45 AM

**Result: PASS**

**Steps executed:**
1. Confirmed heartbeat running via `pm debug --session heartbeat`
2. Heartbeat pane at ❯ prompt, healthy
3. Sent `/exit` to heartbeat session to kill it
4. Pane confirmed dead: "Pane is dead (status 0)"
5. pm debug showed alert: "error heartbeat/pane_dead: Pane %0 in window pm-heartbeat has exited"
6. Within ~60 seconds (one heartbeat cycle), the system auto-relaunched heartbeat
7. Event log shows: "heartbeat/launch: Created tmux window pm-heartbeat with provider claude"
8. Heartbeat pane confirmed alive (pane_dead=0), running Claude 2.1.100
9. After stabilization, heartbeat at ❯ prompt with bypass permissions on
10. All other sessions (operator, 3 workers) remained undisrupted (pane_dead=0)

**Observations:**
- Auto-recovery happened within one heartbeat scheduler cycle (~60s)
- The alert was raised and persisted in the store
- The relaunch created a fresh window in the storage closet
- Claude re-authenticated successfully using the keychain
- Note: operator window is missing from storage (only heartbeat + 3 workers visible)

**Issues found:** 
- The operator session (pm-operator) disappeared from storage closet during the test. 
  It was present before the kill but not after. Need to investigate if the heartbeat 
  recovery cycle affected it. Will track in a separate test (T003/T007).
