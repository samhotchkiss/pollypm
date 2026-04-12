# 0030 Worker nudge on stall via heartbeat

## Problem
When a worker has been idle for 5+ heartbeat cycles, the heartbeat only tells the operator. It should send a direct nudge to the stalled worker to resume work.

## Acceptance Criteria
- Heartbeat detects workers idle for 5+ consecutive cycles
- Sends a direct nudge message to the worker like: "You appear stalled. State the remaining task in one sentence, execute the next step now."
- Nudge respects lease (skip if human holds lease)
- Add test coverage for nudge logic
- Run `uv run pytest -q` and confirm all tests pass

## Reference
System state doc item #5 (P1).
