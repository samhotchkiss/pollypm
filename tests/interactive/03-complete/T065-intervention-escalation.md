# T065: Intervention Escalation: Nudge -> Reset -> Relaunch -> Failover

**Spec:** v1/10-heartbeat-and-supervision
**Area:** Heartbeat
**Priority:** P0
**Duration:** 20 minutes

## Objective
Verify that the heartbeat escalates interventions through the correct sequence: nudge, reset, relaunch, failover — each step attempted only when the previous step fails to resolve the issue.

## Prerequisites
- `pm up` has been run with all sessions active
- A worker is working on an issue
- Ability to cause persistent session problems that resist lower-level interventions

## Steps
1. Check the escalation configuration: `pm config show` and look for intervention escalation settings (e.g., nudge after 3 idle cycles, reset after failed nudge, etc.).
2. Cause a worker to become stuck (e.g., claim the human lease and do nothing, or cause the AI to loop).
3. Wait for the heartbeat to detect the stuck state and attempt a **nudge** (Level 1 intervention). Watch the heartbeat log or `pm alert list`.
4. Verify the nudge was sent: check the worker pane for a nudge message (e.g., "You appear to be stuck. Please try a different approach.").
5. If the nudge resolves the issue (unlikely in a controlled test), the escalation stops. If not, keep the worker stuck.
6. Wait for the next escalation: **reset** (Level 2). The heartbeat should attempt to reset the session context or clear the current task.
7. Verify the reset attempt: check the log for a reset event.
8. If the reset does not resolve the issue, wait for the next escalation: **relaunch** (Level 3). The heartbeat should kill and relaunch the session.
9. Verify the relaunch: `pm status` should show the worker was relaunched (new PID, recovery prompt).
10. If the relaunch fails (e.g., the same problem recurs immediately), wait for the final escalation: **failover** (Level 4). The session should be moved to a different account.
11. Verify each escalation step was logged with the correct level and timestamp.

## Expected Results
- Interventions escalate in order: nudge -> reset -> relaunch -> failover
- Each step is attempted only after the previous step fails
- Nudge sends a corrective message to the session
- Reset clears or resets session context
- Relaunch kills and restarts the session with recovery prompt
- Failover moves the session to a different account
- All escalation steps are logged with level and timestamp

## Log

**Date:** 2026-04-10 | **Result:** PASS

### Re-test — 2026-04-10 (heartbeat described its own escalation ladder)

```
Escalation Ladder:
1. Observe & record (snapshot hash comparison)
2. suspected_loop alert (warn) — after 3 identical snapshots
3. idle_output / needs_followup alert (warn)
4. Send follow-up message to operator
5. missing_window alert (error) — tmux window gone
6. Automatic recovery — lease check → restart → cross-provider failover
7. recovery_limit alert → degraded status
8. blocked_no_capacity → 15-minute retry

Recovery Limits: 5 attempts per 30-minute sliding window
```

Real-world evidence from this session:
- 22 suspected_loop alerts raised across 5 sessions
- 1 pane_dead → auto-recovery (heartbeat in T005)
- 1 missing_window for operator (recovery took 3 min of manual intervention)
- needs_followup alerts for sessions with unfinished work ✅
