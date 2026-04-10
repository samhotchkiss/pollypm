# T062: Looping Detected via Repeated Command Patterns

**Spec:** v1/10-heartbeat-and-supervision
**Area:** Heartbeat
**Priority:** P1
**Duration:** 15 minutes

## Objective
Verify that the heartbeat detects a looping session — one that is producing output but repeating the same commands or patterns without making forward progress.

## Prerequisites
- `pm up` has been run with a worker active
- Knowledge of the loop detection mechanism (e.g., pattern matching on pane output)
- Ability to cause a worker to enter a loop

## Steps
1. Run `pm status` and confirm a worker is active.
2. Attach to a worker session and cause it to enter a loop. Options:
   - Give it a task that causes it to repeatedly try and fail the same approach
   - Provide a circular set of instructions that reference each other
   - Create an issue like "Fix the test" where the test always fails with the same error
3. Observe the worker repeating the same commands or patterns.
4. Detach and monitor the heartbeat output for loop detection.
5. Wait for the heartbeat to analyze the pane output and detect the repeated pattern.
6. Check `pm status` — the worker should be classified as "looping" once the pattern is detected.
7. Verify an alert was generated: `pm alert list` and look for a looping alert with the session ID.
8. Verify the alert includes details about the detected loop pattern (e.g., "repeated command: git commit" or "same error seen N times").
9. Check if the heartbeat takes intervention action (e.g., sending a nudge to break the loop).
10. Observe whether the intervention succeeds in breaking the loop.

## Expected Results
- Heartbeat detects repeated command patterns in pane output
- Session is classified as "looping" when the pattern threshold is exceeded
- An alert is generated with loop pattern details
- The heartbeat may attempt automatic intervention (nudge)
- Loop detection does not false-positive on legitimate repeated commands (e.g., test re-runs)

## Log
