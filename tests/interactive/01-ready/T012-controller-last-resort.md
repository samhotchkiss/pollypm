# T012: Controller Account Used as Last Resort for Workers

**Spec:** v1/02-configuration-and-accounts
**Area:** Account Failover
**Priority:** P1
**Duration:** 15 minutes

## Objective
Verify that the controller account is only used for worker sessions as a last resort, after all other non-controller accounts are exhausted or unhealthy.

## Prerequisites
- At least two accounts configured: one controller, one non-controller
- `pm up` has been run
- Ability to make all non-controller accounts unhealthy simultaneously

## Steps
1. Run `pm account list` and identify the controller account and all non-controller accounts.
2. Run `pm status` and confirm sessions are running normally with workers assigned to non-controller accounts.
3. Make ALL non-controller accounts unhealthy: corrupt their credentials, set them all to cooldown, or otherwise disable them.
4. Wait for the next heartbeat cycle (up to 30 seconds).
5. Run `pm status` and observe worker sessions. They should be failing over.
6. Since no non-controller accounts are available, verify the system escalates to using the controller account for worker sessions.
7. Run `pm account list` and confirm the controller account is now assigned to both its primary role AND the worker session(s).
8. Verify a warning or alert is logged indicating that the controller account is being used as a last resort.
9. Restore one non-controller account to healthy status.
10. Wait for the next heartbeat cycle and verify the worker session is migrated back to the restored non-controller account, freeing the controller.

## Expected Results
- Controller account is only used for workers when NO other healthy accounts are available
- A warning/alert is generated when the controller account is used for workers
- Once a non-controller account becomes healthy again, workers are migrated back
- Controller account retains its primary role throughout the process

## Log
