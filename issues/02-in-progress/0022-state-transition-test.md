# 0022 State Transition Test

## PM Review — 2026-04-10

**Verdict:** Accepted with caveats.

The state transition test (T035) passed: the issue file was successfully moved through all 6 directory states (00-not-ready through 05-completed) and independently verified at the final location.

**Caveats:**
1. **No API-level validation.** Transitions were performed via raw `mv` (file operations), not through the task backend API. This confirms the directory layout works but does not exercise any programmatic state machine.
2. **No guard rails tested.** T035's spec calls for verifying that invalid transitions are rejected and that skip-state attempts are caught. Neither was tested — the file-based tracker has no such enforcement. This is a known gap, not a test failure.
3. **Issue file is a stub.** This file contains only a title. Future completed issues should carry at least a one-line summary of what was done and a link to the test log.

**Recommendation:** Open a follow-up issue to add transition validation to the file-based tracker (reject backward moves, enforce sequential states or require explicit skip confirmation).

## Rework Required — 2026-04-10

**Moved back to in-progress.** This issue is incomplete and needs the following before it can be re-submitted for review:

1. **Acceptance criteria.** Define what "done" looks like — which transitions must be tested, what constitutes a pass/fail, and whether API-level or file-level operations are in scope.
2. **Implementation details.** Document what was actually built or changed, not just that files were moved between directories. If the answer is "nothing was built," state that clearly and reframe the issue as the work it actually requires (e.g., adding transition validation to the file tracker).
3. **Test evidence.** Include concrete output (command runs, assertions, error messages) proving the acceptance criteria were met — not just "verified: file exists."
