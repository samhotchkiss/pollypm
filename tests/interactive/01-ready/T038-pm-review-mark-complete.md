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
