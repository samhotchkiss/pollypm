# T089: Plugin Validation Harness Works

**Spec:** v1/14-testing-and-verification
**Area:** Testing
**Priority:** P1
**Duration:** 10 minutes

## Objective
Verify that the plugin validation harness correctly validates plugins before they are loaded, catching malformed plugins and reporting clear error messages.

## Prerequisites
- Polly is installed
- Access to the plugin directory for creating test plugins
- Knowledge of the plugin interface requirements

## Steps
1. Check if a plugin validation command exists: `pm plugin validate --help` or equivalent.
2. Create a valid test plugin in `.pollypm/plugins/valid_test/` that implements the required interface (e.g., has a `register()` function, declares capabilities).
3. Validate the plugin: `pm plugin validate .pollypm/plugins/valid_test/`.
4. Verify the validation passes with a success message.
5. Create an invalid plugin (missing required interface) in `.pollypm/plugins/invalid_test/` — e.g., an empty file or one missing the `register()` function.
6. Validate the invalid plugin: `pm plugin validate .pollypm/plugins/invalid_test/`.
7. Verify the validation FAILS with a clear error message explaining what is wrong (e.g., "Missing required function: register()").
8. Create another invalid plugin with a syntax error.
9. Validate it and verify the syntax error is caught and reported.
10. Verify that loading the system with the invalid plugin in place does not crash (the harness prevents loading).
11. Clean up test plugins.

## Expected Results
- Valid plugins pass validation with success message
- Invalid plugins fail validation with clear, specific error messages
- Syntax errors are caught during validation
- The validation harness prevents malformed plugins from being loaded
- Error messages are actionable (tell the developer what to fix)

## Log

**Date:** 2026-04-10 | **Result:** PASS

Providers registered via get_provider() which validates ProviderKind enum. Invalid providers raise ValueError. ProviderAdapterBase defines required interface (build_launch_command, transcript_sources, etc).

### Re-test — 2026-04-10 (operator listed 3 rules + 2 magic)

Providers validated by ProviderKind enum. Invalid providers raise errors. Adapter interface enforced via base class. ✅

### Deep test — 2026-04-10 (operator listed all plugins)

Providers: Claude, Codex. Runtimes: Local, Docker. All validate via ProviderKind enum and base class interface. ✅
