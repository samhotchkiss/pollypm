# T006: Override Hierarchy Works (Built-in < User-Global < Project-Local)

**Spec:** v1/01-architecture-and-domain
**Area:** Configuration
**Priority:** P1
**Duration:** 15 minutes

## Objective
Verify that the configuration override hierarchy is correctly enforced: built-in defaults are overridden by user-global settings, which are overridden by project-local settings.

## Prerequisites
- Polly is installed with default built-in configuration
- Access to the user-global config directory (e.g., `~/.config/pollypm/`)
- Access to the project-local config (e.g., `.pollypm/pollypm.toml`)
- Know at least one setting that exists at all three levels (e.g., `heartbeat_interval` or a similar tunable)

## Steps
1. Run `pm config show` and note the effective value for a key setting (e.g., `heartbeat_interval`). Record this as the "built-in default."
2. Confirm the setting is not overridden at user-global or project-local levels yet.
3. Create or edit the user-global config file (e.g., `~/.config/pollypm/config.toml`) and set the same key to a different value (e.g., `heartbeat_interval = 45`).
4. Run `pm config show` and verify the effective value now reflects the user-global override, not the built-in default.
5. Create or edit the project-local config file (e.g., `.pollypm/pollypm.toml`) and set the same key to yet another different value (e.g., `heartbeat_interval = 15`).
6. Run `pm config show` and verify the effective value now reflects the project-local override, superseding both built-in and user-global.
7. Remove the project-local override (delete the key from `.pollypm/pollypm.toml`).
8. Run `pm config show` and verify the effective value falls back to the user-global value.
9. Remove the user-global override (delete the key from user-global config).
10. Run `pm config show` and verify the effective value falls back to the built-in default.

## Expected Results
- Built-in default is the baseline when no overrides exist
- User-global config overrides built-in defaults
- Project-local config overrides user-global config
- Removing project-local override falls back to user-global
- Removing user-global override falls back to built-in default
- `pm config show` accurately reflects the effective merged configuration at each step

## Log
