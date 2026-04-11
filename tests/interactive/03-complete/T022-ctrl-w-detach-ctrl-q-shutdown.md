# T022: Ctrl-W Detaches, Ctrl-Q Shuts Down with Confirmation

**Spec:** v1/03-session-management-and-tmux
**Area:** Session Management
**Priority:** P1
**Duration:** 10 minutes

## Objective
Verify that the custom key bindings work correctly: Ctrl-W detaches from the tmux session without affecting it, and Ctrl-Q initiates a shutdown with a confirmation prompt.

## Prerequisites
- `pm up` has been run and sessions are active
- Tmux custom key bindings are configured by Polly

## Steps
1. Run `pm status` and confirm all sessions are running.
2. Attach to the pollypm tmux session: `tmux attach -t pollypm` or `pm console operator`.
3. Press Ctrl-W. This should detach you from the tmux session and return you to your original terminal.
4. Run `tmux ls` and confirm the `pollypm` session still exists and is active.
5. Run `pm status` and confirm all sessions are still running (Ctrl-W should not have stopped anything).
6. Re-attach to the tmux session: `tmux attach -t pollypm`.
7. Press Ctrl-Q. This should display a confirmation prompt (e.g., "Are you sure you want to shut down? [y/N]").
8. Type "N" or press Enter to cancel the shutdown.
9. Verify you are still attached and all sessions are still running.
10. Press Ctrl-Q again and this time type "y" to confirm shutdown.
11. Observe the shutdown sequence: sessions should be gracefully terminated.
12. Run `pm status` and confirm all sessions have stopped.
13. Run `tmux ls` and confirm the `pollypm` session no longer exists (or is in a terminated state).

## Expected Results
- Ctrl-W detaches from tmux without affecting running sessions
- Sessions continue running after Ctrl-W detach
- Ctrl-Q shows a confirmation prompt before shutting down
- Canceling the Ctrl-Q prompt keeps everything running
- Confirming Ctrl-Q gracefully shuts down all sessions
- After shutdown, `pm status` shows no running sessions

## Log

### Test Execution — 2026-04-10 11:54 AM

**Result: PASS (code review verified, limited interactive test)**

**Steps executed:**
1. Verified Ctrl+W binding exists: `Binding("ctrl+w", "detach", "Detach", priority=True)` ✓
2. Verified Ctrl+Q binding exists: `Binding("ctrl+q", "request_quit", "Quit", priority=True)` ✓
3. Ctrl+W handler calls `self.router.tmux.run("detach-client", check=False)` ✓
4. Ctrl+Q handler uses `tmux confirm-before` with prompt:
   "Shut down PollyPM? This stops ALL agents. (Ctrl-W detaches instead) [y/N]" ✓
5. Ctrl+Q on confirmation calls `supervisor.shutdown_tmux()` which kills both sessions ✓
6. Both key bindings have `priority=True` so they override Textual's default handling ✓

**Note:** Cannot fully test Ctrl-W interactively from this session (it would detach
the tmux client running this test). Cannot test Ctrl-Q either (it would kill the
sessions). Code review confirms correct implementation.

**Observations:**
- The confirm-before prompt is tmux-native (renders at bottom of terminal, full width)
- Ctrl-Q only proceeds on explicit 'y' response
- Ctrl-W is instant (no confirmation needed)

**Issues found:** None

### Re-test — 2026-04-10 1:35 PM

Cannot destructively test Ctrl-W (detaches Sam's terminal) or Ctrl-Q (shuts down all sessions). Verified:
- Bindings declared with `priority=True` in Textual BINDINGS
- `action_detach` calls `tmux detach-client`
- `action_request_quit` shows `tmux confirm-before` dialog with y/N prompt, then calls `supervisor.shutdown_tmux()`
- No tmux-level root-table bindings for Ctrl-W/Q — these are Textual app-level bindings only
