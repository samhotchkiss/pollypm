# T020: Session Recovery with Checkpoint and Recovery Prompt

**Spec:** v1/03-session-management-and-tmux
**Area:** Session Recovery
**Priority:** P0
**Duration:** 15 minutes

## Objective
Verify that when a session crashes and is relaunched, it receives a recovery prompt that includes the last checkpoint data, allowing it to resume work from where it left off.

## Prerequisites
- `pm up` has been run with a worker actively processing an issue
- The worker has been running long enough to have at least one checkpoint recorded

## Steps
1. Run `pm status` and confirm a worker is actively working on an issue.
2. Check the checkpoint store for the worker's latest checkpoint. Look in the state database or `.pollypm/checkpoints/` directory.
3. Note the checkpoint content — it should include the issue being worked on, progress notes, and recent actions.
4. Kill the worker session forcefully: identify the PID and run `kill -9 <PID>`, or `tmux send-keys -t pollypm:worker-0 C-c` followed by termination.
5. Wait for the system to detect the crash and initiate recovery (up to 60 seconds). Monitor via `pm status`.
6. Observe the worker session being relaunched. Attach to it via `pm console worker-0`.
7. Look at the initial prompt or input that the recovered session received. It should be a recovery prompt, not a fresh start.
8. Verify the recovery prompt includes:
   - The issue ID and title being worked on
   - A summary of progress from the checkpoint
   - Recent actions or context
   - Instructions to continue from where it left off
9. Observe the recovered worker continuing to work on the same issue, not starting a new one.
10. Verify a recovery event was logged in the state store or event log.

## Expected Results
- Crashed worker is automatically relaunched
- Recovery prompt is injected into the new session
- Recovery prompt contains checkpoint data (issue, progress, context)
- Worker resumes work on the same issue, not starting fresh
- Recovery event is recorded in the event log
- No work is lost beyond the last checkpoint

## Log
