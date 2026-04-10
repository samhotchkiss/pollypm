# T017: Lease Model - Human Input Claims Lease, Automation Defers

**Spec:** v1/03-session-management-and-tmux
**Area:** Lease Management
**Priority:** P0
**Duration:** 10 minutes

## Objective
Verify that when a human attaches to a session and begins typing, the lease system recognizes human input and prevents automation from sending commands until the human releases the lease.

## Prerequisites
- `pm up` has been run and a worker session is active with automation running
- The worker session is in an automated state (processing an issue or idle waiting)

## Steps
1. Run `pm status` and confirm a worker session is running in automated mode.
2. Observe the worker session briefly via `pm console worker-0` — note that automation is actively producing output or waiting for its next action.
3. Detach without typing anything.
4. Now re-attach to the worker session: `pm console worker-0`.
5. Type a command or message into the session (e.g., start typing a question or instruction to the AI).
6. Observe that the system recognizes human input — there should be an indicator that the lease has been claimed by the human (check `pm status` from another terminal, or look for a lease indicator in the session).
7. From a second terminal, run `pm status` and verify the worker session shows "human-controlled" or "lease: human" or equivalent.
8. While the human lease is active, verify that automation does NOT send any commands to this session. Watch for 60 seconds — no automated input should appear.
9. Continue interacting with the session as a human for a few commands.
10. Verify the lease remains with the human throughout the interaction.

## Expected Results
- Automation runs normally when no human is attached
- Human input immediately claims the lease
- `pm status` reflects the lease holder change
- Automation defers and does not send commands while human holds the lease
- Human can interact freely without interference from automation

## Log
