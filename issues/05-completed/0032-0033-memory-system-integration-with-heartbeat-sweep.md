# 0033 Memory system integration with heartbeat sweep

## Problem
The memory_entries and memory_summaries tables exist in SQLite but are empty. The heartbeat sweep should record project learnings automatically.

## Acceptance Criteria
- During heartbeat sweep, extract notable learnings from session snapshots
- Store them in the memory_entries table via the memory backend
- Deduplicate entries (don't store the same learning twice)
- Add test coverage for memory recording during heartbeat
- Run `uv run pytest -q` and confirm all tests pass

## Reference
System state doc item #8 (P2).
