# 0010 Service API And Frontend Transport

## Goal

Create a stable service API layer so the TUI, future Web UI, and Discord bot all speak to the same core.

## Scope

- in-process command/query interface
- event subscription API
- transport-ready shape for WebSocket / Discord gateways
- TUI migration off direct supervisor coupling where practical

## Acceptance Criteria

- The TUI can use the service API for at least one non-trivial workflow.
- The architecture supports a web frontend and Discord frontend without duplicating business logic.

