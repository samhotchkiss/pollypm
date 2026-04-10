# T025: Plugin Failure Caught at Boundary, Doesn't Crash Core

**Spec:** v1/04-extensibility-and-plugins
**Area:** Plugin System
**Priority:** P0
**Duration:** 10 minutes

## Objective
Verify that when a plugin throws an exception or fails, the error is caught at the plugin boundary and does not crash the core system or other plugins.

## Prerequisites
- `pm up` has been run and sessions are active
- Ability to create a custom plugin that intentionally raises an error
- Access to the project-local plugin directory

## Steps
1. Run `pm status` and confirm all sessions are running normally.
2. Create a faulty test plugin in the project-local plugin directory (`.pollypm/plugins/faulty_test/`). The plugin should register a hook that raises an exception (e.g., `raise RuntimeError("intentional test failure")`).
3. Run `pm down && pm up` to load the faulty plugin.
4. Check the startup log or output for the plugin loading — the faulty plugin should be loaded but its error should be caught.
5. Run `pm status` and verify all core sessions (heartbeat, operator, worker) are still running despite the faulty plugin.
6. Run `pm plugin list` and check if the faulty plugin shows a "failed" or "error" status.
7. Trigger the hook that the faulty plugin registered (e.g., if it hooks into session events, create a session event).
8. Verify the error is logged but does not crash the session or core process. Check `pm log --filter error` or the log files.
9. Verify other plugins continue to function normally (e.g., the claude provider plugin still handles sessions).
10. Clean up: remove the faulty plugin directory and restart.

## Expected Results
- Faulty plugin's exception is caught at the boundary
- Core system continues running without interruption
- Other plugins are unaffected by the faulty plugin's errors
- Error is logged with the plugin name and exception details
- `pm status` shows all core sessions as healthy despite the plugin failure

## Log
