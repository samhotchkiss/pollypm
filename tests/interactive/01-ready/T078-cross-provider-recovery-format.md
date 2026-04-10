# T078: Cross-Provider Recovery Reformats Correctly

**Spec:** v1/12-checkpoints-and-recovery
**Area:** Recovery
**Priority:** P1
**Duration:** 15 minutes

## Objective
Verify that when a recovery requires switching providers (e.g., Claude to Codex), the recovery prompt is reformatted to match the new provider's expected input format while preserving all essential context.

## Prerequisites
- At least two providers configured (Claude and Codex)
- A worker session running on one provider with checkpoints
- Ability to force failover to the other provider

## Steps
1. Confirm a worker is running on Provider A (e.g., Claude) with checkpoints available.
2. Note the checkpoint content and format (it should be in Provider A's native format).
3. Force failover to Provider B (e.g., Codex) by making Provider A's account unhealthy.
4. Enable debug logging to capture the recovery prompt.
5. Wait for the heartbeat to detect the failure and initiate cross-provider recovery.
6. Check the debug log for the recovery prompt sent to Provider B.
7. Verify the recovery prompt has been reformatted:
   - Provider-specific syntax (e.g., XML tags for Claude, different format for Codex) is adjusted
   - System prompt structure matches Provider B's expected format
   - Tool/function call syntax is adapted for Provider B
8. Verify all essential checkpoint data is preserved in the reformatted prompt:
   - Issue ID and title
   - Progress summary
   - Recent actions
   - Files modified
9. Attach to the recovered session on Provider B and verify it can continue working.
10. Verify the session does not produce errors related to format mismatches (e.g., no "I don't understand this format" responses).

## Expected Results
- Recovery prompt is reformatted for the target provider
- Provider-specific syntax is correctly adapted
- All essential checkpoint data is preserved
- Recovered session on the new provider functions normally
- No format-related errors or confusion in the recovered session

## Log
