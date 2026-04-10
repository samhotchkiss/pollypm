# T068: Universal Rules Loaded for All Personas

**Spec:** v1/11-agent-personas-and-prompts
**Area:** Prompts
**Priority:** P0
**Duration:** 10 minutes

## Objective
Verify that universal rules (applicable to all personas and roles) are loaded at session startup and included in every session's system prompt.

## Prerequisites
- `pm up` has been run with sessions active
- Universal rules exist (check `.pollypm/rules/universal.md` or equivalent)

## Steps
1. Locate the universal rules file: check `.pollypm/rules/universal.md`, `~/.config/pollypm/rules/universal.md`, or the built-in rules location.
2. Read the universal rules: `cat <universal-rules-path>`. Note a distinctive rule or phrase.
3. Enable debug logging to see prompt assembly: `pm config set log_level debug`.
4. Restart a session to trigger prompt assembly.
5. Check the debug log for the prompt assembly output. Verify the universal rules content appears in the system prompt.
6. Check the operator session's prompt: the universal rules should be included alongside the persona-specific rules.
7. Check a worker session's prompt: the same universal rules should also be included.
8. Verify the heartbeat session (if it has a prompt) also includes the universal rules.
9. Attach to a worker and ask it about a specific universal rule (e.g., "What are the rules about code style?" if that is a universal rule). The worker should know about it.
10. Verify that universal rules are loaded once and shared across all sessions (not duplicated or loaded separately per session).

## Expected Results
- Universal rules file exists and is accessible
- Universal rules are included in every session's system prompt
- Both operator and worker sessions have the universal rules
- Workers can act on universal rules when relevant
- Rules are loaded consistently across all sessions

## Log
