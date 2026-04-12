# 0028 Deduplicate scheduler jobs on cockpit restart

## Problem
7 duplicate heartbeat jobs accumulate when the cockpit restarts. `ensure_heartbeat_schedule` should clean stale/duplicate jobs, not just check if one exists.

## Acceptance Criteria
- On cockpit restart, existing duplicate scheduler jobs are cleaned up
- Only one heartbeat job and one knowledge_extract job remain after restart
- Add test coverage for the dedup logic
- Run `uv run pytest -q` and confirm all tests pass

## Reference
System state doc item #2 (P0). Launch blocker.
