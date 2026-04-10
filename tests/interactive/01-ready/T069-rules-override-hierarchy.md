# T069: Rules Override Hierarchy (Project-Local Wins)

**Spec:** v1/11-agent-personas-and-prompts
**Area:** Prompts
**Priority:** P1
**Duration:** 15 minutes

## Objective
Verify that when rules with the same name exist at multiple levels (built-in, user-global, project-local), the project-local version takes precedence.

## Prerequisites
- `pm up` has been run
- Access to rules directories at all three levels

## Steps
1. Identify a rule that exists at the built-in level. Check `pm rules list` or the built-in rules directory.
2. Note the content of the built-in rule.
3. Create a user-global override: write a rule with the same name to `~/.config/pollypm/rules/<rule-name>.md` with different content (include "USER-GLOBAL" marker text).
4. Restart sessions and check the debug log or prompt assembly. Verify the user-global version is used instead of the built-in version.
5. Attach to a worker and ask about the rule. It should reference the user-global content.
6. Create a project-local override: write a rule with the same name to `.pollypm/rules/<rule-name>.md` with different content (include "PROJECT-LOCAL" marker text).
7. Restart sessions and check the prompt assembly. Verify the project-local version is used.
8. Attach to a worker and ask about the rule. It should reference the project-local content.
9. Remove the project-local override and restart. Verify the system falls back to the user-global version.
10. Remove the user-global override and restart. Verify the system falls back to the built-in version.

## Expected Results
- Project-local rules override user-global and built-in rules with the same name
- User-global rules override built-in rules
- Removing a higher-precedence rule causes fallback to the next level
- The override hierarchy is: built-in < user-global < project-local
- Only one version of each rule is active at a time (no merging)

## Log
