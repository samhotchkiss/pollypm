# 0016 Agent Profile And Identity Backend

## Goal

Extract agent behavior and identity into a pluggable profile backend so Polly, heartbeat, PA, and future specialist agents can be tuned without hardcoding prompts.

## Scope

- built-in role profiles
- user-local and project-local profile overrides
- provider/model preference hooks
- behavior, capability, memory, and review policy hooks
- composable profile layers

## Acceptance Criteria

- PollyPM can resolve an agent profile separately from provider/model choice.
- Built-in and local custom profiles use the same interface.
- Project-specific agent behavior can be overridden without editing core prompts directly.

