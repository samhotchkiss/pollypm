# T071: Only One Active Rule at a Time

**Spec:** v1/11-agent-personas-and-prompts
**Area:** Prompts
**Priority:** P1
**Duration:** 10 minutes

## Objective
Verify that when multiple versions of the same rule exist at different precedence levels, only one version is active in the final prompt (no duplicates or conflicts).

## Prerequisites
- Rules exist at multiple precedence levels (from T069 setup or similar)
- `pm up` has been run with sessions active

## Steps
1. Create a rule at all three levels with the same name (e.g., "code-style"):
   - Built-in: "Use 2-space indentation" (if modifiable, or note the existing content)
   - User-global: `~/.config/pollypm/rules/code-style.md` with "Use 4-space indentation"
   - Project-local: `.pollypm/rules/code-style.md` with "Use tabs for indentation"
2. Restart sessions to reload rules.
3. Enable debug logging and check the prompt assembly output.
4. Search for all three versions in the assembled prompt: "2-space", "4-space", and "tabs."
5. Verify that ONLY the project-local version ("tabs") appears in the prompt.
6. Verify the built-in and user-global versions are NOT in the prompt (no duplicates).
7. Attach to a worker and ask "What indentation style should I use?" The worker should answer based on the project-local rule only.
8. Remove the project-local rule and restart.
9. Verify only the user-global version is now in the prompt (not both user-global and built-in).
10. Remove the user-global rule and verify only the built-in version remains.

## Expected Results
- Only one version of each named rule is active in the prompt
- The highest-precedence version wins (project-local > user-global > built-in)
- No duplicate or conflicting rule content in the prompt
- Workers follow only the active version of the rule
- Removing higher-precedence rules activates the next level down

## Log
