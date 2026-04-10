# T099: Full Recovery Scenario: Crash Session, Verify Auto-Recovery with Correct Checkpoint

**Spec:** End-to-End Workflows
**Area:** Recovery
**Priority:** P0
**Duration:** 20 minutes

## Objective
Verify the complete recovery scenario end-to-end: a worker actively processing an issue crashes, the heartbeat detects it, the system relaunches the session with the correct recovery prompt from the latest checkpoint, and the worker resumes work without starting over.

## Prerequisites
- `pm up` has been run with all sessions active
- A worker is actively working on a multi-step issue (to have meaningful checkpoints)
- The worker has been running long enough to have at least one Level 1 checkpoint

## Steps
1. Run `pm status` and confirm a worker is actively working on an issue. Note the issue ID and worker session ID.
2. Attach to the worker and observe its progress. Note what step it is currently on (e.g., "completing step 2 of 4").
3. Detach from the worker.
4. Check the latest checkpoint: `pm checkpoint list --session <session-id> --limit 1`. Note the checkpoint level and contents.
5. Record the current state of files the worker has created so far (e.g., `ls` in the worktree).
6. Kill the worker process: `kill -9 <worker-PID>`.
7. Start a timer. Monitor `pm status` every 10 seconds.
8. Within 30 seconds: verify the heartbeat detects the crash (worker shows as "exited").
9. Within 60 seconds: verify the system initiates recovery (worker shows as "recovering" or "relaunching").
10. Within 90 seconds: verify the worker is back up (shows as "running" or "healthy").
11. Attach to the recovered worker session.
12. Observe the recovery prompt or the worker's first actions. It should:
    - Reference the issue it was working on
    - Acknowledge the progress made so far
    - Continue from the checkpoint (e.g., "I was on step 2, continuing with step 3")
    - NOT start the issue from scratch
13. Wait for the worker to complete the remaining steps.
14. Verify the final result includes all work (both pre-crash and post-recovery).
15. Check the event log for the complete recovery timeline: crash detected -> recovery initiated -> session relaunched -> work resumed.

## Expected Results
- Crash is detected within one heartbeat cycle
- Recovery is initiated automatically
- Recovery prompt includes checkpoint data with correct progress state
- Worker resumes from the checkpoint, not from scratch
- Pre-crash work (files, progress) is preserved
- Post-recovery work builds on pre-crash progress
- The complete recovery timeline is logged
- Total recovery time is under 90 seconds

## Log
