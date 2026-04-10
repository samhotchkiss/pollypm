# T021: Cross-Provider Recovery Reformats Prompt Correctly

**Spec:** v1/03-session-management-and-tmux
**Area:** Session Recovery
**Priority:** P1
**Duration:** 15 minutes

## Objective
Verify that when a session fails over from one provider (e.g., Claude) to another (e.g., Codex), the recovery prompt is correctly reformatted to match the target provider's expected input format.

## Prerequisites
- At least two different providers configured (e.g., one Claude account, one Codex account)
- `pm up` has been run with a worker on the Claude provider actively working
- Ability to force failover to a Codex provider

## Steps
1. Run `pm account list` and identify a Claude account and a Codex account.
2. Run `pm status` and confirm a worker is running on the Claude account, actively working on an issue.
3. Wait for a checkpoint to be recorded for this worker.
4. Force the Claude account to become unhealthy (disable credentials, trigger cooldown, etc.) so that failover to the Codex account is required.
5. Wait for the heartbeat to detect the failure and initiate failover (up to 60 seconds).
6. Observe the worker being relaunched on the Codex account. Attach to the session via `pm console worker-0`.
7. Examine the recovery prompt sent to the Codex session. Verify it has been reformatted for the Codex provider:
   - Codex may expect different prompt structure than Claude
   - System instructions may be formatted differently
   - Tool-use syntax may differ
8. Verify the recovery prompt still contains all essential checkpoint data (issue ID, progress, context) despite the format change.
9. Observe the worker resuming work on the same issue using the Codex provider.
10. Restore the Claude account and verify the system returns to normal.

## Expected Results
- Failover triggers when the Claude account becomes unhealthy
- Recovery prompt is reformatted for the Codex provider's expected input format
- All checkpoint data is preserved in the reformatted prompt
- Worker resumes the same issue on the new provider without confusion
- The reformatted prompt does not contain provider-specific syntax from the old provider

## Log
