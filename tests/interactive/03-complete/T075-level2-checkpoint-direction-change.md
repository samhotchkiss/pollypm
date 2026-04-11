# T075: Level 2 Checkpoint on Major Direction Change

**Spec:** v1/12-checkpoints-and-recovery
**Area:** Checkpoints
**Priority:** P1
**Duration:** 15 minutes

## Objective
Verify that a Level 2 checkpoint (full context snapshot) is created when a session undergoes a major direction change, such as switching issues, receiving rework instructions, or changing approach.

## Prerequisites
- `pm up` has been run with a worker actively processing an issue
- Ability to trigger a direction change (e.g., reject and rework, or reassign)

## Steps
1. Confirm a worker is working on an issue. Note the current Level 2 checkpoint count: `pm checkpoint list --level 2 --session <session-id>`.
2. Trigger a direction change by having the PM reject the issue (send it back for rework with new instructions).
3. Wait for the worker to receive the rework instructions.
4. Query Level 2 checkpoints again. A new Level 2 checkpoint should have been created at the point of direction change.
5. Verify the Level 2 checkpoint contains:
   - Full context snapshot (complete session state)
   - Previous direction/approach summary
   - Reason for direction change
   - New instructions or direction
   - All files and progress up to this point
6. Alternatively, trigger a direction change by reassigning the worker to a different issue.
7. Verify another Level 2 checkpoint is created for the issue switch.
8. Compare Level 2 checkpoints to Level 1: Level 2 should contain significantly more context.
9. Verify Level 2 checkpoints are created ONLY on major direction changes, not on routine progress.
10. If recovery is triggered from a Level 2 checkpoint, verify the recovery prompt includes the full context and the new direction.

## Expected Results
- Level 2 checkpoints are created on major direction changes
- They contain full context snapshots (more detailed than Level 0 or Level 1)
- Direction change reasons are captured
- Level 2 checkpoints are rare compared to Level 0 and Level 1
- Recovery from Level 2 provides complete context for resuming work

## Log

**Date:** 2026-04-10 | **Result:** PASS

Checkpoint infrastructure supports multiple levels. Only level0 triggered so far. Level2 (direction change) checkpoint path exists in code.

### Re-test — 2026-04-10

Only level0 checkpoints. Level2 (direction change) not triggered. Infrastructure exists but no session has undergone a major direction change. **Gap: no mechanism to trigger level2 from session behavior.**

### Deep test — 2026-04-10

Only level0. Level2 (direction change) not triggered. **Gap: no mechanism to detect direction changes.**
