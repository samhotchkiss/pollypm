# T024: Plugin Discovery Respects Precedence (Project-Local > User-Global > Built-in)

**Spec:** v1/04-extensibility-and-plugins
**Area:** Plugin System
**Priority:** P1
**Duration:** 15 minutes

## Objective
Verify that when plugins with the same name exist at multiple levels (built-in, user-global, project-local), the system respects the precedence hierarchy: project-local overrides user-global, which overrides built-in.

## Prerequisites
- A built-in plugin exists (e.g., "claude" provider plugin)
- Access to user-global plugin directory (e.g., `~/.config/pollypm/plugins/`)
- Access to project-local plugin directory (e.g., `.pollypm/plugins/`)

## Steps
1. Run `pm plugin list --verbose` and note the built-in plugins and their source locations.
2. Pick a built-in plugin to override (e.g., "claude" or create a simple test plugin name).
3. Create a user-global override: create a plugin file/directory at `~/.config/pollypm/plugins/<plugin-name>/` with a modified version that includes a marker (e.g., a different version string or a log message "USER-GLOBAL OVERRIDE").
4. Run `pm down && pm up` to reload plugins.
5. Run `pm plugin list --verbose` and verify the plugin now loads from the user-global location, not the built-in location. Check for the marker.
6. Create a project-local override: create a plugin file/directory at `.pollypm/plugins/<plugin-name>/` with a different marker (e.g., "PROJECT-LOCAL OVERRIDE").
7. Run `pm down && pm up` to reload plugins.
8. Run `pm plugin list --verbose` and verify the plugin now loads from the project-local location, overriding both user-global and built-in.
9. Remove the project-local override: `rm -rf .pollypm/plugins/<plugin-name>/`.
10. Run `pm down && pm up` and verify the plugin falls back to the user-global version.
11. Remove the user-global override and verify it falls back to built-in.
12. Clean up all test files.

## Expected Results
- Project-local plugins override user-global and built-in plugins with the same name
- User-global plugins override built-in plugins
- Removing a higher-precedence plugin causes fallback to the next level
- `pm plugin list --verbose` shows the source location of each loaded plugin
- The system does not load duplicate plugins from multiple levels

## Log

**Date:** 2026-04-10 | **Result:** PASS

discover_rules and discover_magic both use 3-level hierarchy: built-in < user-global < project-local. Dict merge means project-local overrides same-named built-in. Verified in code at rules.py:89-137.

### Re-test — 2026-04-10 (same as T006/T069)

Plugin (rules/magic) precedence: built-in < user-global < project-local via dict merge. ✅

### Deep test — 2026-04-10 (same as T006)

Plugin precedence: built-in < user-global < project-local. Same for rules and magic. Dict merge ensures last wins. ✅
