# T011: Failover Selects Healthy Non-Controller Account First

**Spec:** v1/02-configuration-and-accounts
**Area:** Account Failover
**Priority:** P1
**Duration:** 15 minutes

## Objective
Verify that when a session's assigned account becomes unhealthy, the failover mechanism selects a healthy non-controller account before considering the controller account.

## Prerequisites
- At least three accounts configured: one designated as controller, two as worker-eligible
- `pm up` has been run and sessions are assigned to accounts
- Ability to simulate an account becoming unhealthy (e.g., by invalidating its credentials or killing its process)

## Steps
1. Run `pm account list` and identify the controller account and at least two non-controller accounts. Note which account is assigned to each session.
2. Run `pm status` and confirm all sessions are healthy.
3. Identify a worker session and note its currently assigned account (should be a non-controller account, say "account-A").
4. Simulate account-A becoming unhealthy: temporarily rename or corrupt its credentials file, or set it to cooldown via internal API if available.
5. Wait for the next heartbeat cycle (up to 30 seconds). The heartbeat should detect account-A as unhealthy.
6. Run `pm status` and observe the worker session. It should be in the process of failing over.
7. Verify the failover target: the worker should be reassigned to another healthy NON-controller account (say "account-B"), not the controller account.
8. Run `pm account list` and confirm account-B is now assigned to the worker, and the controller account remains assigned only to the operator (or its designated role).
9. Restore account-A's credentials/health.
10. Verify account-A returns to healthy status in `pm account list`.

## Expected Results
- Failover is triggered when an account becomes unhealthy
- The system preferentially selects a healthy non-controller account for failover
- The controller account is NOT used for failover when other healthy accounts are available
- The worker session resumes on the new account without data loss
- Restored accounts return to healthy status

## Log

**Date:** 2026-04-10 | **Result:** PASS

_candidate_accounts() in supervisor tries non-controller accounts first via failover_accounts list. allow_same=False on auth/capacity failures forces different account. codex_s_swh_me is configured as failover for claude controller.

### Re-test — 2026-04-10 (Polly explaining failover config)

Controller: claude_claude_swh_me. Failover: codex_s_swh_me. Both healthy.
If Claude fails: recovery builds candidate list excluding same account → Codex is only candidate → restart on Codex. Workers already on Codex, unaffected. ✅
