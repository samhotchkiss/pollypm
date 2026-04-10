# T088: Bug Fix Includes Failing-First Test

**Spec:** v1/14-testing-and-verification
**Area:** Testing
**Priority:** P1
**Duration:** 15 minutes

## Objective
Verify that bug fixes in the codebase follow the "failing-first" test methodology: a test that reproduces the bug is written first (fails before the fix, passes after).

## Prerequisites
- Access to git history to identify recent bug fixes
- pytest is installed

## Steps
1. Identify a recent bug fix: `git log --oneline --since="2 weeks ago" | grep -i "fix"` and find a bug fix commit.
2. Note the commit hash and message: `git show --stat <commit-hash>`.
3. Identify the test file(s) included in the fix: look for test files in the changed files list.
4. Verify a test file was modified or created as part of the bug fix (not just source code changes).
5. Read the test that was added: it should test the specific scenario that triggered the bug.
6. Verify the test would have failed before the fix:
   - Check out the commit before the fix: `git stash && git checkout <commit-hash>~1`
   - Run the specific test: `pytest <test-file>::<test-name> -v`
   - The test should FAIL (demonstrating the bug)
7. Return to the current code: `git checkout -` (or `git checkout main && git stash pop`).
8. Run the same test on the current code: `pytest <test-file>::<test-name> -v`.
9. The test should now PASS (demonstrating the fix).
10. Verify the test is specific enough to catch the exact bug (not too broad).

## Expected Results
- Bug fix commit includes a test that reproduces the bug
- The test fails on the code before the fix
- The test passes on the code after the fix
- The test is specific to the bug scenario
- The failing-first pattern is consistently followed for bug fixes

## Log
