# 0008 Pluggable Extensibility Architecture

## Goal

Define the plugin, hook/filter, service API, and backend-swapping architecture that lets PollyPM grow into a platform.

## Deliverables

- A reviewable architecture document covering:
  - plugin host
  - hook/filter model
  - frontend/service API split
  - replaceable memory backend
  - replaceable task backend
  - provider/runtime plugin points
  - Web UI and Discord integration seams

## Current Output

- See `docs/extensibility-architecture.md`

## Acceptance Criteria

- The architecture explains how local user plugins survive upgrades.
- The architecture explains how new CLI providers can be added without editing core code.
- The architecture explains how Web UI and Discord can sit on the same service layer as the TUI.
- The architecture explains how memory and task systems can be swapped.

