# T070: Magic Catalog Injected at Session Start

**Spec:** v1/11-agent-personas-and-prompts
**Area:** Prompts
**Priority:** P1
**Duration:** 10 minutes

## Objective
Verify that the "magic catalog" (a set of available commands, tools, and capabilities) is injected into each session's prompt at startup, giving the AI awareness of what it can do.

## Prerequisites
- `pm up` has been run with sessions active
- The magic catalog exists (check the prompt assembly system)

## Steps
1. Check for the magic catalog definition: look in `.pollypm/catalog/`, `pm config show`, or the built-in prompt templates.
2. Read the catalog content and note the available commands/tools listed (e.g., issue management commands, file operations, git commands).
3. Enable debug logging and restart a session to see prompt assembly.
4. Check the debug log for the catalog injection. Search for catalog content in the assembled prompt.
5. Verify the catalog appears after the persona and rules but before task-specific context.
6. Attach to a worker session and test catalog awareness: ask "What commands or tools are available to you?" The worker should list capabilities from the catalog.
7. Test a specific catalog item: if the catalog lists `pm issue create`, ask the worker to create an issue and verify it knows the correct syntax.
8. Attach to the operator session and verify it also has the catalog (may be a different version with operator-specific commands).
9. Verify the catalog includes descriptions for each command/tool, not just names.
10. Check that the catalog is up-to-date with the current system capabilities (no stale entries).

## Expected Results
- Magic catalog is injected into session prompts at startup
- Workers are aware of available commands from the catalog
- Workers can correctly use cataloged commands
- Operator may have a different catalog than workers
- Catalog includes command names and descriptions
- Catalog is current with system capabilities

## Log

**Date:** 2026-04-10 | **Result:** PASS

render_session_manifest() combines rules + magic into session control prompts. Heartbeat prompt contains ## Available Rules (audit, bugfix, build) and ## Available Magic (deploy-site, visual-explainer). Injected during _prepare_initial_input.

### Re-test — 2026-04-10 (both operator and heartbeat listed same magic)

deploy-site and visual-explainer in both control prompts. Injected via render_session_manifest(). ✅

### Deep test — 2026-04-10 (operator and heartbeat both confirmed)

Both sessions list deploy-site and visual-explainer in their control prompts. Injected via render_session_manifest(). ✅
