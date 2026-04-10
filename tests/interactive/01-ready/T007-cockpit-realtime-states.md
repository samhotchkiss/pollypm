# T007: Cockpit Shows Real-Time Session States Correctly

**Spec:** v1/01-architecture-and-domain
**Area:** Observability
**Priority:** P1
**Duration:** 10 minutes

## Objective
Verify that the cockpit (status dashboard) displays accurate, real-time session states for all active roles, updating when states change.

## Prerequisites
- `pm up` has been run and all sessions are healthy
- Familiarity with the cockpit UI (either TUI dashboard or `pm status` output)

## Steps
1. Run `pm status` (or open the cockpit dashboard) and observe the displayed state for each session: heartbeat, operator, and worker(s).
2. Verify each session shows a state label (e.g., "running", "healthy", "idle", "working").
3. Assign an issue to a worker (use `pm issue create --title "Test task" --body "Do a small task"` then wait for the operator to assign it, or manually assign via `pm issue assign <id> worker-0`).
4. Within 30 seconds, re-check `pm status` and verify the worker's state changed (e.g., from "idle" to "working" or "busy").
5. Kill a worker session intentionally (e.g., `kill -9 <worker-PID>` or `tmux send-keys -t pollypm:worker-0 C-c`).
6. Re-check `pm status` within 15 seconds and verify the killed worker shows as "exited" or "unhealthy."
7. Wait for auto-recovery to relaunch the worker (up to 60 seconds).
8. Re-check `pm status` and verify the worker returns to a healthy state.
9. Run `pm down` and check `pm status` — it should show all sessions as stopped or report no active sessions.
10. Run `pm up` and verify all sessions return to running/healthy state in the cockpit.

## Expected Results
- Cockpit shows accurate state for every session role
- State updates reflect within 30 seconds of a change
- Killed sessions show as exited/unhealthy promptly
- Recovered sessions show as running/healthy after relaunch
- `pm down` results in all sessions shown as stopped
- `pm up` restores all sessions to running state in the display

## Log
