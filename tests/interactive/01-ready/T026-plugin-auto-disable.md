# T026: Plugin Auto-Disabled After N Repeated Failures

**Spec:** v1/04-extensibility-and-plugins
**Area:** Plugin System
**Priority:** P1
**Duration:** 15 minutes

## Objective
Verify that a plugin which fails repeatedly (N consecutive failures) is automatically disabled by the system to prevent ongoing disruption.

## Prerequisites
- `pm up` has been run
- Ability to create a custom plugin that fails on every invocation
- Knowledge of the configured failure threshold (N) for auto-disable

## Steps
1. Check the configured failure threshold: run `pm config show` and look for a plugin failure threshold setting (e.g., `plugin_max_failures = 5`). Note the value.
2. Create a plugin in `.pollypm/plugins/repeat_fail/` that hooks into a frequently triggered event (e.g., heartbeat cycle) and raises an exception every time.
3. Run `pm down && pm up` to load the plugin.
4. Run `pm plugin list` and verify the plugin is initially loaded and active.
5. Wait for the plugin to fail N times. Monitor the log: `pm log --filter repeat_fail` or `tail -f <log-dir>/polly.log | grep repeat_fail`.
6. Count the failure occurrences in the log. After the Nth failure, the plugin should be auto-disabled.
7. Run `pm plugin list` and verify the plugin now shows as "disabled" or "auto-disabled."
8. Verify the system logged a message indicating the plugin was auto-disabled due to repeated failures.
9. Verify the hook is no longer being called: watch the log for additional failures — there should be none after the disable.
10. Run `pm status` and confirm all core sessions remain healthy throughout this process.
11. Clean up: remove the plugin directory and restart.

## Expected Results
- Plugin is loaded and active initially
- After N consecutive failures, the plugin is automatically disabled
- A log message clearly indicates the auto-disable and the reason
- The disabled plugin no longer receives hook invocations
- Core system and other plugins remain unaffected
- The failure count matches the configured threshold

## Log
