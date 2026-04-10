# T003: Session Roles Enforce Correct Behavior

**Spec:** v1/01-architecture-and-domain
**Area:** Session Lifecycle
**Priority:** P0
**Duration:** 15 minutes

## Objective
Verify that each session role (heartbeat, operator, worker) enforces its designated behavior: heartbeat monitors health, operator manages workflow and triages, workers execute assigned tasks.

## Prerequisites
- `pm up` has been run and all sessions are healthy
- At least one issue exists in the ready queue (create one with `pm issue create` if needed)
- Terminal with tmux available

## Steps
1. Run `pm status` and note the role assignments for each session.
2. Attach to the heartbeat session via `pm console heartbeat`.
3. Observe the heartbeat output for at least two cycles. Verify it is performing health checks on other sessions (look for health classification output like "healthy", "idle", "stuck").
4. Confirm the heartbeat is NOT executing issues or managing workflow — its output should be limited to monitoring and alerting.
5. Detach and attach to the operator session via `pm console operator`.
6. Observe the operator's behavior. It should be checking the inbox, triaging items, or waiting for items to triage. If there is a ready issue, watch whether the operator assigns it to a worker.
7. Confirm the operator is NOT directly executing code or implementation tasks — it should be delegating to workers.
8. Detach and attach to a worker session via `pm console worker-0`.
9. If the worker has been assigned an issue, observe it executing the task (reading files, writing code, running tests).
10. Confirm the worker is NOT triaging inbox items or managing other sessions.
11. Run `pm status` and verify each role's status label matches its function.

## Expected Results
- Heartbeat session output contains only monitoring/health-check data
- Operator session handles triage, assignment, and review — not implementation
- Worker session handles implementation — not triage or monitoring
- No role bleeds into another role's responsibilities
- `pm status` labels each role correctly

## Log
