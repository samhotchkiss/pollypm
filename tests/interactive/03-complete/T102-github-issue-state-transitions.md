# T102: GitHub Issue State Transitions via polly:* Labels

**Spec:** v1/06-issue-management
**Area:** Issue Management — GitHub Integration
**Priority:** P0
**Duration:** 15 minutes

## Objective
Verify that issues can be moved between states by changing polly:* labels, and that PollyPM correctly tracks these transitions.

## Prerequisites
- pollypm is running
- At least one GitHub issue exists with `polly:ready` label
- `gh` CLI is authenticated

## Steps
1. List issues: `gh issue list --repo samhotchkiss/pollypm --label polly:ready`
2. Pick an issue (e.g., #1)
3. Move it to in-progress: `gh issue edit 1 --remove-label polly:ready --add-label polly:in-progress`
4. Verify in cockpit Issues view that the issue moved to the in-progress section
5. Move it to needs-review: `gh issue edit 1 --remove-label polly:in-progress --add-label polly:needs-review`
6. Verify in cockpit Issues view
7. Move it back to in-progress (reject): `gh issue edit 1 --remove-label polly:needs-review --add-label polly:in-progress`
8. Verify the reject loop works in the display
9. Move it to completed: `gh issue edit 1 --remove-label polly:in-progress --add-label polly:completed`
10. Close the issue: `gh issue close 1`
11. Verify completed issues show or are hidden appropriately
12. Reopen and reset: `gh issue reopen 1 && gh issue edit 1 --remove-label polly:completed --add-label polly:ready`

## Expected Results
- Each label change is reflected in the Issues view
- The reject loop (needs-review → in-progress) works correctly
- Completed/closed issues are handled appropriately

## Log

**Date:** 2026-04-10 | **Result:** BLOCKED (GitHub backend not built)

Requires GitHub issue backend. polly:* labels and state sync not yet implemented.

### Re-test — 2026-04-10

**Result: BLOCKED** — GitHub issue backend not built. Polly confirmed: 'No github.py or any file with github in the name under src/pollypm/. The file-based tracker is the only issue backend currently implemented.'
