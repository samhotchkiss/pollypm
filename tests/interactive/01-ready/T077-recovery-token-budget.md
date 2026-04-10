# T077: Recovery Respects Token Budget with Priority Truncation

**Spec:** v1/12-checkpoints-and-recovery
**Area:** Recovery
**Priority:** P1
**Duration:** 15 minutes

## Objective
Verify that the recovery prompt respects the provider's token budget and uses priority-based truncation when the full recovery context exceeds the limit.

## Prerequisites
- `pm up` has been run
- A worker session with extensive checkpoint history (many Level 0, 1, and 2 checkpoints)
- Knowledge of the provider's token limit

## Steps
1. Check the configured token budget for recovery prompts: `pm config show` and look for `recovery_token_budget` or similar.
2. Create a scenario with extensive context: assign a complex issue with many steps, let the worker work through multiple checkpoints, generating a large context.
3. Kill the worker to trigger recovery.
4. Check the debug log for the recovery prompt assembly. Look for messages about token budget.
5. If the full recovery context exceeds the budget, verify the system truncates lower-priority content first:
   - Priority order should be: identity > current issue > recent progress > older progress > project overview > detailed history
   - The highest-priority content (identity, current issue) should never be truncated
6. Verify the truncated prompt fits within the token budget.
7. Check for truncation markers or indicators in the prompt (e.g., "[truncated]" or "[earlier context omitted]").
8. Verify the recovered session can still function effectively with the truncated prompt (it knows what it is working on and can continue).
9. Compare the truncated prompt to a non-truncated prompt (from a simpler recovery): verify the priority system is working.
10. Test with a very small token budget (if configurable) and verify the system still produces a usable recovery prompt.

## Expected Results
- Recovery prompt respects the configured token budget
- Lower-priority content is truncated first
- Highest-priority content (identity, issue) is never truncated
- Truncation is indicated in the prompt
- Recovered session functions effectively even with truncated context
- Token budget is not exceeded

## Log
