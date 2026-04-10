# T103: Polly Assigns a GitHub Issue to a Worker Session

**Spec:** v1/06-issue-management, v1/11-agent-personas
**Area:** Issue Management — PM/PA Workflow
**Priority:** P0
**Duration:** 20 minutes

## Objective
Verify that Polly (the operator PM) can pick a GitHub issue from the ready queue, assign it to a worker session, and the worker begins execution.

## Prerequisites
- pollypm is running with operator and worker sessions
- GitHub issues exist with `polly:ready` labels
- Worker session for the pollypm project is running

## Steps
1. Open the Polly session in the cockpit
2. Send Polly a message: "Pick the highest priority ready issue and assign it to the worker"
3. Observe Polly's response — she should identify issues by their labels/tiers
4. Verify Polly moves the issue label from polly:ready to polly:in-progress
5. Verify Polly sends the issue details to the worker session
6. Switch to the worker session and verify it received the task
7. Observe the worker beginning to execute the issue
8. Wait for the worker to complete and move the issue to polly:needs-review
9. Switch back to Polly and verify she detects the review request
10. Have Polly review the work and either approve or reject

## Expected Results
- Polly correctly identifies and selects issues from GitHub
- Labels transition correctly throughout the workflow
- Worker receives clear instructions from Polly
- The full PM → PA → review loop works end-to-end

## Log
