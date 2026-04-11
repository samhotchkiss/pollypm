# T074: Level 1 Checkpoint on Meaningful Work Completion

**Spec:** v1/12-checkpoints-and-recovery
**Area:** Checkpoints
**Priority:** P0
**Duration:** 15 minutes

## Objective
Verify that a Level 1 checkpoint is created when a worker completes a meaningful unit of work (e.g., completing a sub-task, passing a test, committing code).

## Prerequisites
- `pm up` has been run with a worker actively processing an issue
- The issue involves multiple steps or sub-tasks

## Steps
1. Create an issue with multiple steps: `pm issue create --title "Multi-step task" --body "Step 1: Create file a.txt. Step 2: Create file b.txt. Step 3: Create file c.txt."`.
2. Move the issue to ready and wait for a worker to pick it up.
3. Query existing Level 1 checkpoints: `pm checkpoint list --level 1 --session <worker-session-id>` or query the database.
4. Note the current count of Level 1 checkpoints for this worker.
5. Observe the worker completing Step 1 (creating a.txt).
6. After Step 1 completion, query Level 1 checkpoints again. A new checkpoint should have been created.
7. Verify the Level 1 checkpoint contains:
   - Checkpoint ID
   - Level: 1
   - Session ID
   - Issue ID
   - Progress summary (e.g., "Completed step 1 of 3")
   - Recent actions taken
   - Files modified
8. Observe the worker completing Step 2. Verify another Level 1 checkpoint is created.
9. Verify each Level 1 checkpoint captures the incremental progress (step 2 checkpoint includes step 1 + step 2 progress).
10. If the worker is recovered from any of these checkpoints, verify the recovery prompt reflects the correct progress level.

## Expected Results
- Level 1 checkpoints are created at meaningful work boundaries
- Each checkpoint captures the current progress and recent actions
- Multiple Level 1 checkpoints accumulate as work progresses
- Checkpoints include enough context for recovery (issue, progress, files)
- Level 1 checkpoints are more detailed than Level 0 but created less frequently

## Log

**Date:** 2026-04-10 | **Result:** PARTIAL PASS

Infrastructure exists: checkpoints table supports levels, checkpoint builder creates level0 on every heartbeat cycle (465 checkpoints). Level1 (meaningful work completion) not yet triggered in this session — requires a worker to complete an issue. The code path exists in the heartbeat backend.

### Re-test — 2026-04-10

Only level0 checkpoints exist (759 of them). Level1 (meaningful work completion) not yet triggered — workers haven't completed issues through the formal pipeline. Infrastructure exists in checkpoint table's level column. **Gap: level1 checkpoint creation not integrated into worker completion flow.**

### Deep test — 2026-04-10 (heartbeat confirmed only level0 exists)

Only level0 checkpoints. Level1 (meaningful work) infrastructure exists but not triggered. **Gap: no integration with worker completion flow.**
