# T023: Built-in Plugins Load at Startup

**Spec:** v1/04-extensibility-and-plugins
**Area:** Plugin System
**Priority:** P0
**Duration:** 10 minutes

## Objective
Verify that all built-in plugins (claude, codex, local_runtime, etc.) are loaded automatically during startup and are functional.

## Prerequisites
- Polly is installed with default configuration
- `pm down` has been run (fresh start)

## Steps
1. Run `pm plugin list` (or equivalent) to see the expected list of built-in plugins before starting.
2. Run `pm up` and observe the startup output for plugin loading messages.
3. After startup completes, run `pm plugin list` again and verify all built-in plugins are loaded:
   - `claude` provider plugin
   - `codex` provider plugin
   - `local_runtime` plugin
   - Any other expected built-in plugins
4. For each listed plugin, check its status: it should show "loaded" or "active."
5. Verify the claude plugin is functional: check that a Claude account can be used for sessions (already verified if sessions are running).
6. Verify the codex plugin is functional: if a Codex account is configured, check that it could be used.
7. Verify the local_runtime plugin is functional: check that local command execution works within sessions.
8. Run `pm plugin info claude` (or equivalent) to see plugin metadata: version, capabilities, hook registrations.
9. Run `pm plugin info codex` and verify similar metadata.
10. Check the startup log for any plugin loading errors: `pm log --filter plugin` or search the log files.

## Expected Results
- All built-in plugins are listed as loaded/active after startup
- No plugin loading errors in the startup log
- Each plugin reports correct metadata (version, capabilities)
- Provider plugins (claude, codex) are functional for session management
- Plugin loading happens automatically without manual intervention

## Log
