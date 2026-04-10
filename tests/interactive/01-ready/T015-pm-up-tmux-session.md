# T015: pm up Creates pollypm Tmux Session with Correct Windows

**Spec:** v1/03-session-management-and-tmux
**Area:** Session Management
**Priority:** P0
**Duration:** 10 minutes

## Objective
Verify that `pm up` creates a tmux session named `pollypm` with the correct window layout: one window per session role, properly named and configured.

## Prerequisites
- No existing `pollypm` tmux session (run `pm down` and `tmux kill-session -t pollypm` if needed)
- Polly is installed and configured with at least one account

## Steps
1. Ensure no pollypm session exists: `tmux ls` should not list `pollypm`.
2. Run `pm up` and wait for it to complete.
3. Run `tmux ls` and confirm a `pollypm` session is listed.
4. Run `tmux list-windows -t pollypm` and record the output. Note each window's index, name, and dimensions.
5. Verify there is a window named for the heartbeat role (e.g., "heartbeat" or "hb").
6. Verify there is a window named for the operator role (e.g., "operator" or "op").
7. Verify there is at least one window named for a worker role (e.g., "worker-0" or "w0").
8. For each window, verify it contains exactly one pane (or the expected number of panes): `tmux list-panes -t pollypm:<window-name>`.
9. Attach to the tmux session (`tmux attach -t pollypm`) and cycle through windows using Ctrl-B N. Verify each window is active and shows the expected session role's output.
10. Detach and run `pm status` to cross-reference the status output with the tmux window list. They should match.

## Expected Results
- `tmux ls` shows the `pollypm` session
- Windows are named according to session roles (heartbeat, operator, worker-N)
- Each window contains the correct number of panes
- All windows are active and showing output from their respective roles
- `pm status` output matches the tmux session layout

## Log
