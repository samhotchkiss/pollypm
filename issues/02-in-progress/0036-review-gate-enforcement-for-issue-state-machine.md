# 0036 Review gate enforcement for issue state machine

## Problem
Issues can skip the 03-needs-review and 04-in-review states, bypassing the review gate. The state machine should enforce that issues pass through review before completion.

## Acceptance Criteria
- Issue transitions enforce the full state machine (no skipping 03/04 states)
- Add validation that blocks direct 02->05 transitions
- The operator (PM role) must explicitly approve before an issue can move to 05-completed
- Add test coverage for transition validation
- Run `uv run pytest -q` and confirm all tests pass

## Reference
System state doc gaps table: "Review gate enforcement — Issues skip 03/04 states".
