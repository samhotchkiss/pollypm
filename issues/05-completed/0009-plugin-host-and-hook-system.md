# 0009 Plugin Host And Hook System

## Goal

Implement the first version of the PollyPM plugin host and lifecycle hook/filter system.

## Scope

- plugin manifest discovery
- built-in vs repo-local vs user-local plugin precedence
- plugin API version checks
- observer and filter hook routing
- safe plugin failure isolation

## Acceptance Criteria

- PollyPM can discover plugins from a user-local directory that will not be overwritten by upgrades.
- Built-in and external plugins use the same manifest and loading path.
- Hooks support both observe-only and mutating/veto behavior.

