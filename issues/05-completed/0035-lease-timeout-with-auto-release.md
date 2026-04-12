# 0035 Lease timeout with auto-release

## Problem
Leases have no timeout — if a session crashes while holding a lease, the lease is never released, blocking heartbeat/recovery indefinitely.

## Acceptance Criteria
- Add a lease timeout (default 30 min, configurable)
- Heartbeat sweep checks for expired leases and auto-releases them
- Record an event when a lease is auto-released due to timeout
- Add test coverage
- Run `uv run pytest -q` and confirm all tests pass

## Reference
System state doc gaps table: "Lease timeout — No auto-release".
