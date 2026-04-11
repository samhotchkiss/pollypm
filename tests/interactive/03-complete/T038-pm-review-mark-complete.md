# T038: PM Reviews Issue and Marks Complete

**Spec:** v1/06-issue-management
**Area:** Issue Review
**Priority:** P0
**Duration:** 10 minutes

## Objective
Verify that when the PM (operator) reviews a completed issue and finds it satisfactory, it marks the issue as done/complete, and the worker is freed for the next task.

## Prerequisites
- `pm up` has been run and all sessions are active
- A worker is working on or has completed an issue that will pass review

## Steps
1. Create a straightforward issue: `pm issue create --title "Simple file creation" --body "Create a file named hello.txt containing the text Hello World"`.
2. Move the issue to ready and wait for a worker to pick it up.
3. Observe the worker completing the task (it should create the file and move the issue to "review").
4. Run `pm issue info <id>` and confirm the status is "review."
5. Attach to the operator session and observe the review process.
6. The operator should check the worker's output (verify hello.txt exists with correct content) and approve the issue.
7. Run `pm issue info <id>` and verify the status transitions to "done."
8. Run `pm status` and verify the worker that was assigned to this issue is now idle or has been assigned a new issue.
9. Verify the issue file on disk reflects the "done" status with the completion timestamp.
10. Run `pm issue list --status done` and confirm the issue appears in the done list.

## Expected Results
- Operator reviews the issue and approves it
- Issue transitions from "review" to "done"
- Worker is released from the issue and becomes available for new work
- Completion timestamp is recorded
- Issue appears in the done list
- The review approval is logged in the event history

## Log

**Date:** 2026-04-10 | **Result:** PASS

### Re-test — 2026-04-10 (BLOCKED — operator session died)

**Result: BLOCKED**

Attempted to ask Polly (operator) to review issue 0022, but the operator session had died. The cockpit showed "The Polly session is not currently running."

**MAJOR BUG: Heartbeat stops monitoring during conversation.** The heartbeat admitted: "My heartbeat sweeps stopped running when I entered this interactive Q&A session with you. The operator window died after my last sweep and I never noticed."

This means:
1. When anyone interacts with the heartbeat session, ALL monitoring stops
2. Sessions can die undetected during heartbeat conversations
3. The heartbeat is a single-threaded Claude session — it can't monitor AND answer questions simultaneously
4. The scheduler can't trigger sweeps while Claude is in an active turn

### Retried after operator recovery — 2026-04-10

After heartbeat recovered the operator (~3 minutes, 15+ tool calls), re-initialized operator with control prompt. Polly reviewed issue 0022 and wrote a detailed review:
```
Verdict: Accepted with caveats.
1. No API-level validation — raw mv, not task backend API
2. No guard rails tested — no enforcement of sequential transitions
3. Issue file is a stub
Recommendation: Follow-up issue for transition validation
```
PM review behavior ✅. Recovery was painfully slow but eventually worked.

**Additional bug:** No simple CLI command to recover dead control sessions (operator, heartbeat). The heartbeat tried pm worker-start, pm up, python scripts before finding a path.
