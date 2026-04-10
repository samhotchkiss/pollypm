# T087: New Feature Has Unit + Integration Tests

**Spec:** v1/14-testing-and-verification
**Area:** Testing
**Priority:** P1
**Duration:** 15 minutes

## Objective
Verify that any recently added feature includes both unit tests (testing individual functions/classes) and integration tests (testing the feature in context with other components).

## Prerequisites
- Access to the git history to identify recent features
- pytest is installed

## Steps
1. Identify a recently added feature: `git log --oneline --since="2 weeks ago" | head -20` and find a feature commit (not a bug fix or refactor).
2. Note the feature name and the files that were changed: `git show --stat <commit-hash>`.
3. Identify the source files for the feature (e.g., `src/pollypm/new_feature.py`).
4. Search for corresponding unit tests: `find tests/ -name "*new_feature*"` or `grep -r "new_feature" tests/unit/`.
5. Verify unit test files exist for the feature.
6. Read the unit tests and verify they test individual functions/methods:
   - At least one test per public function
   - Edge cases covered (empty input, None, errors)
   - Assertions check specific expected values
7. Search for corresponding integration tests: `find tests/ -name "*new_feature*"` in the integration test directory, or `grep -r "new_feature" tests/integration/`.
8. Verify integration tests exist and test the feature interacting with other components.
9. Run only the new feature's tests: `pytest tests/ -k "new_feature" -v`.
10. Verify all feature tests pass.

## Expected Results
- Unit tests exist for the new feature
- Integration tests exist for the new feature
- Unit tests cover individual functions with edge cases
- Integration tests verify the feature works with other components
- All feature tests pass
- Test coverage for the new feature is adequate

## Log
