# T018: Lease Timeout Returns Control to Automation After 30 Min

**Spec:** v1/03-session-management-and-tmux
**Area:** Lease Management
**Priority:** P1
**Duration:** 35 minutes

## Objective
Verify that after a human claims a session lease and then stops interacting, the lease times out after 30 minutes and control returns to automation.

## Prerequisites
- `pm up` has been run and a worker session is active
- The lease timeout is configured to 30 minutes (or whatever the configured timeout is)
- Second terminal available for monitoring

## Steps
1. Run `pm status` and confirm a worker session is running in automated mode.
2. Attach to the worker session via `pm console worker-0`.
3. Type a command to claim the lease (e.g., "hello").
4. From a second terminal, run `pm status` and verify the lease shows as "human" for this session.
5. Note the current time. Stop typing and do not interact with the session further.
6. Every 5 minutes, check `pm status` from the second terminal to monitor the lease status. The lease should remain "human" during the timeout period.
7. At the 25-minute mark, verify the lease is still "human" but may show a "lease expiring soon" indicator.
8. At the 30-minute mark (or configured timeout), check `pm status` again. The lease should have reverted to "automation."
9. Observe the worker session — automation should resume sending commands or processing its queue.
10. Attach to the worker session again and verify automation is active (producing output without human input).

## Expected Results
- Human lease is held for the full timeout duration without activity
- `pm status` shows lease transitions: human -> automation after timeout
- Automation resumes automatically after lease timeout
- No manual intervention required to return control to automation
- The session continues functioning normally after the transition

## Log
