# T093: Existing Plugins Load After Core Changes

**Spec:** v1/15-migration-and-stability
**Area:** Migration
**Priority:** P1
**Duration:** 10 minutes

## Objective
Verify that existing plugins (both built-in and custom) continue to load and function correctly after core system updates, ensuring backward compatibility of the plugin interface.

## Prerequisites
- A custom plugin is installed (user-global or project-local)
- Built-in plugins are present
- The custom plugin was working before the update

## Steps
1. Run `pm plugin list` and record all loaded plugins and their statuses.
2. Verify the custom plugin is listed and functional: `pm plugin info <custom-plugin-name>`.
3. Record the plugin interface version (if available): `pm plugin info <custom-plugin-name>` may show a compatibility version.
4. Perform a core update (e.g., `git pull && pip install -e .` or `pip install --upgrade pollypm`).
5. Run `pm plugin list` and verify ALL previously loaded plugins are still listed.
6. Verify each plugin's status is "loaded" or "active" (not "error" or "incompatible").
7. Check the startup log for any plugin compatibility warnings: `pm log --filter plugin`.
8. Test the custom plugin's functionality: trigger a hook or operation that the custom plugin handles.
9. Verify built-in plugins (claude, codex, local_runtime) are still functional.
10. Run `pm up` and verify sessions start correctly, indicating provider plugins are working.

## Expected Results
- All previously loaded plugins load successfully after the core update
- No "incompatible" or "error" statuses for any plugin
- Custom plugins function correctly after the update
- Built-in plugins function correctly after the update
- No plugin-related errors in the startup log
- Sessions start and run normally with the updated core

## Log

**Date:** 2026-04-10 | **Result:** PASS

ClaudeAdapter and CodexAdapter load without errors. LocalRuntimeAdapter and DockerRuntimeAdapter available. All providers is_available() returns True. Verified after multiple code changes.

### Re-test — 2026-04-10 (operator confirmed both Claude + Codex adapters working)

ClaudeAdapter, CodexAdapter, LocalRuntimeAdapter, DockerRuntimeAdapter all load. ✅

### Deep test — 2026-04-10 (processes confirmed running)

Claude sessions run as 2.1.101. Codex sessions run as node. All adapters functional. ✅
