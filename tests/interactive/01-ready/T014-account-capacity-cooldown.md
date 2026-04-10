# T014: Account Capacity Tracked and Cooldown Enforced

**Spec:** v1/02-configuration-and-accounts
**Area:** Account Management
**Priority:** P1
**Duration:** 15 minutes

## Objective
Verify that the system tracks account usage capacity (e.g., rate limits, token budgets) and enforces cooldown periods when an account exceeds its limits.

## Prerequisites
- At least two accounts configured
- `pm up` has been run
- Knowledge of the capacity/cooldown thresholds configured for the accounts

## Steps
1. Run `pm account list --verbose` (or equivalent) and note the current usage and capacity for each account.
2. Identify an account and note its remaining capacity or rate limit status.
3. Trigger heavy usage on that account by assigning multiple tasks or sending many requests through a worker session assigned to it.
4. Monitor the account's usage counter: run `pm account list --verbose` periodically and watch the usage numbers increase.
5. Continue until the account hits its capacity limit or rate limit.
6. Observe the system's response: the account should enter a "cooldown" state.
7. Run `pm account list` and verify the account shows a "cooldown" status with an expected expiry time.
8. Verify that no new sessions are assigned to the cooldown account: if a new issue is created, the worker should use a different account.
9. Wait for the cooldown period to expire (or manually advance time if the system supports it).
10. Run `pm account list` and verify the account returns to "available" or "healthy" status after cooldown expires.

## Expected Results
- Account usage is tracked and visible via `pm account list --verbose`
- Hitting capacity triggers cooldown state
- Cooldown status shows expiry time
- No new sessions assigned to cooldown accounts
- Account returns to available after cooldown expires
- Existing sessions on the account are migrated if needed during cooldown

## Log
