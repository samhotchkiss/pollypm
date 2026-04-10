# T001: Fresh Install and pm up Launches All Three Session Roles

**Spec:** v1/01-architecture-and-domain
**Area:** Session Lifecycle
**Priority:** P0
**Duration:** 10 minutes

## Objective
Verify that running `pm up` on a fresh installation correctly launches all three session roles (heartbeat, operator, worker) inside a pollypm tmux session.

## Prerequisites
- Polly is installed but has not been started (`pm down` if previously running)
- At least one Claude or Codex account is configured
- No existing `pollypm` tmux session (`tmux kill-session -t pollypm` if needed)
- Terminal with tmux available

## Steps
1. Open a fresh terminal window.
2. Run `pm down` to ensure no prior sessions exist.
3. Run `tmux ls` and confirm no `pollypm` session is listed.
4. Run `pm up` and observe the output. It should indicate that the pollypm tmux session is being created.
5. Wait for the startup sequence to complete (watch for the "all sessions launched" or equivalent confirmation message).
6. Run `tmux ls` and confirm a `pollypm` session now exists.
7. Run `tmux list-windows -t pollypm` and verify there are windows for each role.
8. Attach to the heartbeat window: `tmux select-window -t pollypm:heartbeat` (or the equivalent window name). Confirm the heartbeat process is running and producing output.
9. Attach to the operator window: `tmux select-window -t pollypm:operator`. Confirm the operator session is active.
10. Attach to a worker window: `tmux select-window -t pollypm:worker-0` (or equivalent). Confirm the worker session is active.
11. Run `pm status` and verify all three roles report as "running" or "healthy".

## Expected Results
- `pm up` exits without errors
- `tmux ls` shows a `pollypm` session
- Three distinct session roles are visible as tmux windows or panes
- `pm status` reports all roles as running
- Heartbeat window shows periodic health-check output
- Operator window shows an active operator prompt or idle state
- Worker window shows an active worker prompt or idle state

## Log

### Test Execution — 2026-04-10 11:43 AM

**Result: PASS**

**Steps executed:**
1. Killed existing pollypm and pollypm-storage-closet sessions
2. Verified clean state: no pollypm sessions
3. Cleared session markers for clean launch
4. Ran `pm` — output: Creating heartbeat, operator, worker_pollypm, worker_otter_camp, worker_pollypm_website
5. Verified `pollypm` session created (1 window - PollyPM cockpit)
6. Verified `pollypm-storage-closet` session created (5 windows)
7. Confirmed all 5 session windows: pm-heartbeat, pm-operator, worker-pollypm, worker-otter_camp, worker-pollypm-website
8. Heartbeat: running at ❯ prompt with bypass permissions on
9. Operator: running at ❯ prompt with bypass permissions on
10. Workers: running at › prompt (codex), 100% capacity
11. `pm debug` shows all 5 sessions as "running" with correct providers

**Observations:**
- Bootstrap creates 5 sessions (2 control + 3 workers) matching config
- All Claude sessions have --dangerously-skip-permissions active
- All Codex sessions at interactive prompt
- Cockpit rail shows all projects with correct indicators
- 1 open alert (stale from previous session): "operator/needs_followup"
- Heartbeat scheduled at 1-minute intervals

**Issues found:** None
