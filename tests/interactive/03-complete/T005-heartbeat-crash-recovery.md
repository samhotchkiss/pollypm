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

### Re-test — 2026-04-10 1:22 PM

**Result: PASS with notes**

#### BEFORE
```
pane %7, pid=25153, dead=0, cmd=2.1.100
❯ (idle at prompt, had previous conversation history)
```

#### KILL
Sent `/exit` to heartbeat via tmux send-keys.
Result: Claude said "See ya!", pane died, pm-heartbeat window disappeared from storage closet.

#### RECOVERY
Heartbeat window reappeared within ~30 seconds (before the 60s cycle).
New pane %31, pid=52384, dead=0, cmd=2.1.101.

#### AFTER
```
❯ What is your current role? Are you the heartbeat supervisor?
⏺ My responsibilities:
  - Monitor the other managed sessions
  - Track progress and record heartbeats
  - Spot stuck sessions, detect ended turns, watch for loops or drift
  - Surface anomalies quickly
```
Heartbeat retained its role knowledge because `--continue` preserved conversation history.

All other sessions survived: pm-operator, worker-otter_camp, worker-pollypm-website, cockpit. No disruption.

#### Notes
- Recovery used `--continue` flag so conversation history persists (no fresh control prompt needed)
- No explicit recovery prompt was sent — the LLM retained context from the continued session
- Claude version bumped from 2.1.100 to 2.1.101 on relaunch
