# T079: Rapid Failure Escalation After N Attempts

**Spec:** v1/12-checkpoints-and-recovery
**Area:** Recovery
**Priority:** P0
**Duration:** 15 minutes

## Objective
Verify that if a session repeatedly fails recovery (crashes again immediately after relaunch), the system escalates rapidly rather than retrying indefinitely.

## Prerequisites
- `pm up` has been run with sessions active
- Ability to cause a session to crash immediately after relaunch (e.g., corrupted environment, invalid credentials)
- Knowledge of the rapid failure threshold (N attempts)

## Steps
1. Check the rapid failure escalation configuration: `pm config show` and look for `max_recovery_attempts` or similar (e.g., N = 3).
2. Create a condition that causes a worker to crash immediately after relaunch:
   - Corrupt the worker's account credentials
   - Set an invalid working directory
   - Create a crash-on-startup condition
3. Kill the worker to trigger the first recovery attempt.
4. Watch the heartbeat log as the system attempts to relaunch the worker.
5. The relaunched worker should crash immediately. Count: Attempt 1.
6. The heartbeat detects the crash and tries again. The worker crashes again. Count: Attempt 2.
7. Continue observing recovery attempts until the system reaches the threshold (N).
8. After N failed attempts, verify the system escalates:
   - May try failover to a different account
   - May disable the worker session with an alert
   - Should NOT retry indefinitely
9. Check `pm alert list` for an escalation alert with details about the repeated failure.
10. Resolve the underlying issue (fix credentials, etc.) and verify the system can be manually recovered.

## Expected Results
- System attempts recovery up to N times
- After N failed attempts, the system escalates (failover or disable)
- System does NOT retry indefinitely (no infinite crash loop)
- An alert is generated explaining the repeated failure
- The failure count matches the configured threshold
- Manual recovery is possible after fixing the root cause

## Log
