# T041: Report Status Shows Correct Counts per State

**Spec:** v1/06-issue-management
**Area:** Issue Tracking
**Priority:** P1
**Duration:** 10 minutes

## Objective
Verify that the issue report/status command shows correct counts of issues in each state (open, ready, in_progress, review, done, closed).

## Prerequisites
- Several issues exist in various states
- `pm up` is running or the issue tracker is accessible

## Steps
1. Create a known set of issues in different states. For example:
   - 2 issues in "open" state
   - 2 issues in "ready" state
   - 1 issue in "in_progress" state
   - 1 issue in "review" state
   - 2 issues in "done" state
   - 1 issue in "closed" state
2. Run `pm issue list` and manually count issues per state to establish the expected counts.
3. Run `pm report status` (or `pm issue report` or equivalent command that shows counts).
4. Verify the report shows the correct count for each state:
   - Open: 2
   - Ready: 2
   - In Progress: 1
   - Review: 1
   - Done: 2
   - Closed: 1
   - Total: 9
5. Transition one issue from "open" to "ready."
6. Re-run the report and verify the counts updated (Open: 1, Ready: 3).
7. Create a new issue and verify the total count increases by 1.
8. Close an issue and verify the "closed" count increases and the source state count decreases.
9. Run `pm issue list --status <each-state>` for each state and verify the individual listings match the report counts.
10. Verify the report includes a total count that matches the sum of all individual state counts.

## Expected Results
- Report shows accurate counts for each issue state
- Counts update immediately after state transitions
- Total count equals the sum of all state counts
- Individual `--status` filters match the report counts
- Report format is clear and readable

## Log

**Date:** 2026-04-10 | **Result:** PASS

state_counts() returns: 00-not-ready=0, 01-ready=1, 02-in-progress=0, 03-needs-review=0, 04-in-review=0, 05-completed=20. Total=21. Matches filesystem state.

### Re-test — 2026-04-10 (Polly counting issue files)

```
│ 00-not-ready    │ 0  │
│ 01-ready        │ 2  │
│ 02-in-progress  │ 1  │
│ 03-needs-review │ 0  │
│ 04-in-review    │ 0  │
│ 05-completed    │ 20 │
│ Total           │ 23 │
```
Review stages (03, 04) never used — issues skip to completed. **BUG: No review gate enforced.** ✅
