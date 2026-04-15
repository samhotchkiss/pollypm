# 0029 Clean cockpit state on restart

## Problem
When the cockpit starts, cockpit_state.json may contain stale pane IDs and mounted session references from a previous run, blocking recovery.

## Acceptance Criteria
- On cockpit start, validate cockpit_state.json entries
- Check that right_pane_id and mounted_session point to real, alive panes
- Clear stale entries automatically
- Add test coverage for state validation logic
- Run `uv run pytest -q` and confirm all tests pass

## Reference
System state doc item #7 (P1). Launch blocker.
