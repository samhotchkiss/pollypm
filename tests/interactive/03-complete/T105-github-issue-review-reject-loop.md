# T105: GitHub Issue Review and Reject Loop End-to-End

**Spec:** v1/06-issue-management
**Area:** Issue Management — Review Workflow
**Priority:** P0
**Duration:** 25 minutes

## Objective
Verify the complete review loop: worker completes an issue, Polly reviews it, finds problems, rejects it back to the worker, worker fixes the problems, resubmits, and Polly approves.

## Prerequisites
- pollypm is running with Polly and a worker session
- A GitHub issue is in polly:in-progress state
- Worker is actively working on the issue

## Steps
1. Wait for the worker to move an issue to polly:needs-review
2. Verify a GitHub comment is added with handoff notes
3. Open the Polly session
4. Tell Polly to review the issue
5. Polly should move it to polly:in-review
6. Polly reviews the work — tell her to find a specific problem
7. Polly should reject: move to polly:in-progress with review comments
8. Verify GitHub shows the rejection comment
9. Switch to worker — verify it picks up the review feedback
10. Worker addresses the feedback and moves back to polly:needs-review
11. Polly reviews again
12. This time tell Polly to approve
13. Polly moves to polly:completed and closes the issue
14. Verify the full history is visible in GitHub comments

## Expected Results
- The reject → fix → resubmit → approve loop works fully
- Each transition is recorded as a GitHub comment
- Labels update correctly at each step
- The issue ends up closed with polly:completed label

## Log

**Date:** 2026-04-10 | **Result:** BLOCKED (GitHub backend not built)

Requires GitHub backend with comment-based review/reject workflow.

### Re-test — 2026-04-10

**Result: BLOCKED** — GitHub issue backend not built. Polly confirmed: 'No github.py or any file with github in the name under src/pollypm/. The file-based tracker is the only issue backend currently implemented.'
