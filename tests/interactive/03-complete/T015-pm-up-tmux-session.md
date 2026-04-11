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

**Date:** 2026-04-10
**Result:** PASS

### Execution

1. **tmux ls** — Two sessions: `pollypm` (cockpit, 1 window "PollyPM" with 2 panes: rail + mounted session) and `pollypm-storage-closet` (session storage, 5 windows).

2. **Windows verified:**
   - `pm-heartbeat` (1 pane) — Claude at prompt, role=heartbeat-supervisor ✅
   - `pm-operator` (1 pane) — Claude at prompt, role=operator-pm ✅
   - `worker-pollypm` (1 pane) — Codex at prompt, role=worker ✅
   - `worker-otter_camp` (1 pane) — role=worker ✅
   - `worker-pollypm-website` (1 pane) — role=worker ✅

3. **Cockpit layout** — PollyPM window has 2 panes: left=cockpit rail (Textual TUI), right=mounted session view.

4. **pm status** — All 5 sessions listed with correct role labels matching tmux windows. ✅

### Re-test — 2026-04-10 1:42 PM

**Result: PASS with bugs**

#### Current tmux layout
```
pollypm: 1 window
  PollyPM (2 panes): %28 uv 0x30 | %1 2.1.100 31x172

pollypm-storage-closet: 5 windows
  0 pm-heartbeat (1 pane) dead=0
  2 worker-pollypm (1 pane) dead=0
  3 worker-otter_camp (1 pane) dead=0
  4 worker-pollypm-website (1 pane) dead=0
  5 PollyPM (2 panes) dead=0  ← STALE
```

#### Bugs found
1. **pm-operator window (1) MISSING** from storage closet — the operator pane was mounted in the cockpit but the storage window was destroyed rather than kept as a placeholder
2. **Stale PollyPM window (5)** in storage closet — leftover from earlier cockpit debugging, contains dead uv and bash panes
