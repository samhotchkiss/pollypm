# T039: Issue Counter Increments Atomically

**Spec:** v1/06-issue-management
**Area:** Issue Tracking
**Priority:** P1
**Duration:** 10 minutes

## Objective
Verify that the issue counter increments atomically and does not produce duplicate IDs, even when multiple issues are created in rapid succession.

## Prerequisites
- Polly is installed and the issue tracker is functional
- Current issue count is known

## Steps
1. Run `pm issue list` and note the highest existing issue number (e.g., ISS-005).
2. Create 10 issues in rapid succession using a shell loop:
   ```
   for i in $(seq 1 10); do pm issue create --title "Rapid issue $i" --body "Testing atomic counter" & done; wait
   ```
3. Wait for all commands to complete.
4. Run `pm issue list` and list all newly created issues.
5. Verify that exactly 10 new issues were created (no more, no fewer).
6. Verify that all 10 issue IDs are unique — no duplicates.
7. Verify that the IDs are sequential (e.g., ISS-006 through ISS-015 with no gaps).
8. Check the issue files on disk: `ls .pollypm/issues/` and count the files. The count should match.
9. Verify no error messages were produced during rapid creation.
10. Run `pm issue list` one more time and verify the total count is correct (previous count + 10).

## Expected Results
- All 10 rapid issue creations succeed
- No duplicate issue IDs are generated
- Issue IDs are sequential with no gaps
- Issue files on disk match the expected count
- No race conditions or errors during concurrent creation
- The counter correctly reflects the total number of issues

## Log
