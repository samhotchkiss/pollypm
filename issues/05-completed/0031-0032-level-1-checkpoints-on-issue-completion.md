# 0032 Level 1 checkpoints on issue completion

## Problem
Only Level 0 checkpoints (raw pane snapshots) exist. When a worker completes an issue (file moves to 05-completed), a Level 1 checkpoint with a work summary should be created.

## Acceptance Criteria
- Detect when an issue transitions to 05-completed
- Create a Level 1 checkpoint with a summary of the work done (files changed, tests added/modified)
- Store in the existing checkpoint infrastructure (SQLite)
- Add test coverage
- Run `uv run pytest -q` and confirm all tests pass

## Reference
System state doc item #9 (P2).
