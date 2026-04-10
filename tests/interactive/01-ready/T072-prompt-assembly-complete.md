# T072: Prompt Assembly Includes All Components

**Spec:** v1/11-agent-personas-and-prompts
**Area:** Prompts
**Priority:** P0
**Duration:** 10 minutes

## Objective
Verify that the fully assembled prompt for a session includes all required components in the correct order: persona, universal rules, active rules, magic catalog, project overview, and task-specific context.

## Prerequisites
- `pm up` has been run with sessions active
- Debug logging enabled to inspect prompt assembly
- A worker is assigned an issue (for task-specific context)

## Steps
1. Enable debug logging: `pm config set log_level debug`.
2. Restart a worker session that has an assigned issue.
3. Locate the prompt assembly output in the debug log. Search for the full assembled prompt.
4. Verify the prompt includes the following components in order:
   a. **Persona definition** — the assigned persona's role and behavior guidelines
   b. **Universal rules** — rules that apply to all sessions
   c. **Active rules** — persona-specific or project-specific rules (respecting override hierarchy)
   d. **Magic catalog** — available commands and tools
   e. **Project overview** — project context and documentation summary
   f. **Task-specific context** — the current issue details, checkpoint data, etc.
5. Verify each component is present (not missing or empty).
6. Verify the ordering is consistent (persona first, task context last).
7. Verify no component appears twice (no duplicates).
8. Check the prompt for the operator session as well — it should have the same structure but different persona and no task assignment.
9. Verify the total prompt size is within token budget limits.
10. Compare two worker sessions' prompts — shared components (persona, rules, catalog, overview) should be identical, only task context should differ.

## Expected Results
- All six components are present in the assembled prompt
- Components appear in the correct order
- No duplicates or missing components
- Operator and worker prompts share common components but differ in persona and task context
- Prompt fits within token budget
- Two workers on different tasks have identical shared components

## Log
