# T002: Human Can Attach to Any Session and Type Commands

**Spec:** v1/01-architecture-and-domain
**Area:** Session Lifecycle
**Priority:** P0
**Duration:** 10 minutes

## Objective
Verify that a human operator can attach to any active session (heartbeat, operator, worker) via tmux and interact with it by typing commands.

## Prerequisites
- `pm up` has been run and all sessions are healthy
- `pm status` shows all roles running
- Terminal with tmux available

## Steps
1. Run `pm status` to confirm all sessions are running and note the window/pane names.
2. Attach to the operator session using `pm console operator` (or `tmux attach -t pollypm` then navigate to the operator window).
3. In the operator session, type a simple command (e.g., `/status` or a known operator command). Verify the session accepts input and produces output.
4. Detach from the operator session using Ctrl-B then D (standard tmux detach).
5. Attach to a worker session using `pm console worker-0` (or navigate to the worker window in tmux).
6. In the worker session, type a simple command or message. Verify it accepts input and produces a response.
7. Detach from the worker session.
8. Attach to the heartbeat session using `pm console heartbeat` (or navigate to the heartbeat window).
9. Observe the heartbeat output. Try typing a command if the heartbeat session accepts input. If read-only, confirm that fact.
10. Detach and run `pm status` again to confirm no sessions were disrupted by the attach/detach cycle.

## Expected Results
- Human can attach to each session type without errors
- Typed commands are accepted and produce appropriate responses in operator and worker sessions
- Heartbeat session is observable (output visible)
- Attaching and detaching does not crash or disrupt any session
- `pm status` remains healthy after all attach/detach operations

## Log
