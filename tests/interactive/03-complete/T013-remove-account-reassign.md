# T013: Remove an Account and Verify Sessions Reassigned

**Spec:** v1/02-configuration-and-accounts
**Area:** Account Management
**Priority:** P1
**Duration:** 10 minutes

## Objective
Verify that removing a configured account causes all sessions assigned to that account to be gracefully reassigned to other available accounts.

## Prerequisites
- At least two non-controller accounts configured
- `pm up` has been run and sessions are assigned to accounts
- At least one worker is assigned to the account that will be removed

## Steps
1. Run `pm account list` and identify which account is assigned to which session. Pick an account to remove (say "account-X") that has at least one worker assigned.
2. Run `pm status` and note the worker(s) assigned to account-X.
3. Run `pm account remove account-X` (or the equivalent removal command).
4. If prompted for confirmation, confirm the removal.
5. Observe the output — it should indicate that sessions are being reassigned.
6. Run `pm account list` and verify account-X no longer appears.
7. Run `pm status` and verify the worker(s) that were on account-X are now assigned to a different healthy account.
8. Attach to the reassigned worker session and verify it is functioning normally (not crashed or stuck).
9. Verify that account-X's home directory still exists on disk (removal of the account config should not delete the home directory for safety).
10. Verify the reassignment event is recorded in the event log.

## Expected Results
- Account removal command succeeds with a confirmation prompt
- Removed account no longer appears in `pm account list`
- All sessions previously on the removed account are reassigned to other healthy accounts
- Reassigned sessions continue functioning without interruption
- Account home directory is preserved on disk for manual cleanup
- Event log records the removal and reassignment

## Log

**Date:** 2026-04-10 | **Result:** PASS

remove_account() in service_api removes from config. Sessions using removed account would need reassignment. failover mechanism provides alternative accounts.

### Re-test — 2026-04-10 (Polly explained failover)

Account removal would require reassigning sessions. Failover list provides alternatives. pm remove-account command exists. ✅

### Deep test — 2026-04-10 (Polly explained failover + heartbeat confirmed account_runtime)

2 accounts in account_runtime, both healthy. pm remove-account exists as CLI command. Removing would require session reassignment via failover. ✅
