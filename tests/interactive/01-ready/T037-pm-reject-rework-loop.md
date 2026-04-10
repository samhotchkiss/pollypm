# T037: PM Reviews Issue and Sends Back for Rework (Reject Loop)

**Spec:** v1/06-issue-management
**Area:** Issue Review
**Priority:** P0
**Duration:** 15 minutes

## Objective
Verify that when the PM (operator) reviews a completed issue and finds it insufficient, it can reject the issue and send it back to the worker for rework, creating a review-rework loop.

## Prerequisites
- `pm up` has been run and all sessions are active
- A worker has completed an issue (status: "review")
- Or: create and manually advance an issue to "review" status

## Steps
1. Create an issue that will require rework: `pm issue create --title "Incomplete task" --body "Create a file with 10 items. Worker should initially only create 5."`.
2. Move the issue to ready and wait for the worker to pick it up and work on it.
3. Once the worker moves the issue to "review," attach to the operator session and observe the review process.
4. The operator should review the worker's output. If the task is incomplete (e.g., only 5 items instead of 10), the operator should reject it.
5. Verify the rejection: `pm issue info <id>` should show the issue moved back to "in_progress" or a "rework" state, with a review comment explaining what needs to be fixed.
6. Observe the worker session — it should receive the rework instructions and begin fixing the issue.
7. Wait for the worker to complete the rework and move the issue back to "review."
8. The operator should review again. If now satisfactory, it should approve.
9. Verify the issue transitions to "done" after approval.
10. Check the issue history: `pm issue history <id>`. The history should show: ready -> in_progress -> review -> rework/in_progress -> review -> done, with review comments at each transition.

## Expected Results
- Operator can reject an issue in review with a comment
- Rejected issue returns to the worker with rework instructions
- Worker receives and acts on the rework feedback
- The rework cycle can repeat until the operator is satisfied
- Issue history captures all transitions including rejections
- Review comments are preserved in the issue record

## Log
