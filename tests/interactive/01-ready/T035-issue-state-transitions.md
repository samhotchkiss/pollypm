# T035: Issue Transitions Through All Six States Correctly

**Spec:** v1/06-issue-management
**Area:** Issue Tracking
**Priority:** P0
**Duration:** 15 minutes

## Objective
Verify that an issue can transition through all six lifecycle states (open -> ready -> in_progress -> review -> done -> closed) and that invalid transitions are rejected.

## Prerequisites
- Polly is installed and a project is initialized
- `pm up` has been run (or at least the issue tracker is functional)

## Steps
1. Create a new issue: `pm issue create --title "State transition test"`. Note the issue ID. Verify initial status is "open."
2. Transition to "ready": `pm issue transition <id> ready` (or equivalent command). Run `pm issue info <id>` and verify status is now "ready."
3. Transition to "in_progress": `pm issue transition <id> in_progress`. Verify status is "in_progress."
4. Transition to "review": `pm issue transition <id> review`. Verify status is "review."
5. Transition to "done": `pm issue transition <id> done`. Verify status is "done."
6. Transition to "closed": `pm issue transition <id> closed`. Verify status is "closed."
7. Verify the issue file on disk reflects the final "closed" status.
8. Attempt an invalid transition: try to move a closed issue back to "open" (`pm issue transition <id> open`). This should either be rejected with an error or require explicit confirmation.
9. Create a second issue and attempt to skip states (e.g., go directly from "open" to "review"). Verify whether the system enforces sequential transitions or allows skipping.
10. Check the issue's transition history: `pm issue history <id>` or inspect the issue file for a changelog. All transitions should be recorded with timestamps.

## Expected Results
- Issue successfully transitions through all six states in order
- Each transition updates the status in `pm issue info` and on disk
- Invalid transitions are rejected or flagged with a warning
- Transition history records all state changes with timestamps
- The lifecycle follows the documented state machine

## Log
