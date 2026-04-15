# 0031 Lease integration with cockpit mount/unmount

## Problem
When the cockpit mounts a session pane, it doesn't claim a lease. Human typing in a mounted pane is unprotected from heartbeat interference.

## Acceptance Criteria
- Mounting a session in the cockpit auto-claims a "cockpit" lease
- Unmounting releases the lease
- Heartbeat respects the cockpit lease (skips nudges/recovery for leased sessions)
- Add test coverage
- Run `uv run pytest -q` and confirm all tests pass

## Reference
System state doc item #4 (P1).
