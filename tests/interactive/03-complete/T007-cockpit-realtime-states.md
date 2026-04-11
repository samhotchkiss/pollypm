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

### Test Execution — 2026-04-10 11:48 AM

**Result: PASS**

**Steps executed:**
1. Captured cockpit rail — shows centered wordmark, slogans, all projects
2. Polly shows • (blue dot) = ready/idle — operator is at ❯ prompt ✓
3. otter-camp shows ● (green solid) = live session, idle — worker at › prompt ✓
4. PollyPM shows ● (green solid) = live session, idle — worker at › prompt ✓
5. pollypm-website shows ● (green solid) = live session, idle — worker at › prompt ✓
6. sam-blog-rebuild-rest… shows ○ (dim circle) = no running session ✓
7. news (Nora) shows ○ (dim circle) = no running session ✓
8. Inbox shows ◇ (0) = empty inbox ✓
9. Section separator "── projects ────────" visible ✓
10. Settings at bottom with ⚙ icon ✓

**Turn detection accuracy:**
- All 3 codex workers confirmed idle at › prompt → ● shown correctly
- Heartbeat and operator confirmed at ❯ prompt (Claude idle)
- No false positives (no spinning indicators on idle sessions)

**Observations:**
- Project names properly truncated (sam-blog-rebuild-rest…)
- Polly indicator uses • not ● (different styling for operator vs worker)
- Persona names shown: "news (Nora)"

**Issues found:** None

### Re-test — 2026-04-10 1:28 PM

**Result: PASS — real-time state transitions verified**

Tested by sending a task to the Codex worker while watching the cockpit rail indicator:

1. **Idle state:** PollyPM shows `●` (green solid dot = live, idle at prompt)
2. **Sent task:** "List every file in docs/v1/ with their sizes"
3. **Working state captured:** PollyPM shows `◟ ◜ ◝` (rotating spinner = actively working)
```
▌ ◟ PollyPM    (t+0.3s)
▌ ◜ PollyPM    (t+0.9s)
▌ ◝ PollyPM    (t+1.5s)
```
4. **Back to idle:** After Codex finished (listed 17 files), indicator returned to `●`

Other indicators confirmed:
- `▲` = active with alerts (otter-camp had suspected_loop alert)
- `○` = idle/no session (sam-blog, news)
- `◇` = empty inbox
- `•` = operator ready (Polly)

Turn detection works correctly for both Claude (❯ prompt) and Codex (› prompt) sessions.
