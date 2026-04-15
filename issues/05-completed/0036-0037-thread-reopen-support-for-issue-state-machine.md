# 0037 Thread reopen support for issue state machine

## Problem
The issue state machine is forward-only — once an issue moves to a later state, it cannot be reopened or moved back. This is needed when review finds problems that require rework.

## Acceptance Criteria
- Add a `reopen` transition that moves issues from 04-in-review or 05-completed back to 02-in-progress
- Add a `request-changes` transition from 04-in-review back to 02-in-progress
- Record the transition in the progress log
- Add test coverage for backward transitions
- Run `uv run pytest -q` and confirm all tests pass

## Reference
System state doc gaps table: "Thread reopen — Forward-only state machine".
