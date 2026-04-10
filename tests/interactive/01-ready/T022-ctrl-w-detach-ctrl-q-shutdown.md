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
