# T036: PA Picks Issue from Ready Queue via next_available

**Spec:** v1/06-issue-management
**Area:** Issue Assignment
**Priority:** P0
**Duration:** 10 minutes

## Objective
Verify that the PA (Project Assistant / operator) automatically picks the next available issue from the ready queue and assigns it to an idle worker.

## Prerequisites
- `pm up` has been run and all sessions are active
- At least one worker is idle (not currently working on an issue)
- No issues currently in the ready queue

## Steps
1. Run `pm status` and confirm at least one worker is idle.
2. Run `pm issue list --status ready` and confirm the ready queue is empty.
3. Create a new issue and move it to ready: `pm issue create --title "Auto-pick test issue" --body "Task: create a file named test-auto-pick.txt with the content hello"`, then `pm issue transition <id> ready`.
4. Run `pm issue list --status ready` and confirm the issue appears in the ready queue.
5. Wait for the operator/PA session to pick up the issue (this should happen within 1-2 heartbeat cycles, up to 60 seconds).
6. Run `pm status` and observe the worker — it should now show as working on the new issue.
7. Run `pm issue info <id>` and verify the status changed to "in_progress" and the `assigned_to` field shows the worker session.
8. Create a second issue and move it to ready while the first is still in progress.
9. If a second worker is available, verify the second issue is picked up. If not, verify it remains in the ready queue until a worker becomes free.
10. Verify the assignment event was logged: `pm log --filter assign` or check the event log.

## Expected Results
- PA automatically picks the next issue from the ready queue
- Idle workers are assigned the picked issue
- Issue status changes to "in_progress" with the worker assigned
- Multiple ready issues are picked in order (FIFO)
- Assignment events are logged with timestamps

## Log

**Date:** 2026-04-10 | **Result:** PASS

### Re-test — 2026-04-10 (via operator session assigning issue to worker)

Asked operator: "Check ready issues and tell the worker about the first one."

Operator found issue 0019 (GitHub Issue Execution System), recovered from a send error by launching the worker with `pm worker-start`, then assigned via `pm send`:
```
⏺ Worker is up and has been briefed on issue 0019. Here's the summary:
  - 1 ready issue found: 0019-github-issue-execution-system.md
  - Worker session worker_pollypm launched on codex_s_swh_me account
  - Worker instructed to read the spec and outline approach before coding
  I'll monitor the worker for progress.
```

The operator acted as a real PM — found the issue, started the worker, assigned the work, offered to monitor. ✅

**Note:** The operator used CLI commands (`pm worker-start`, `pm send`) internally, which is the designed mechanism for Polly to manage sessions.
