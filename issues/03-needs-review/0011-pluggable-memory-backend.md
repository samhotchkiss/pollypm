# 0011 Pluggable Memory Backend

## Goal

Extract Prompt Master memory into a replaceable backend contract.

## Scope

- default local memory backend
- backend interface for read/write/compact/summarize
- config-based backend selection
- hook points around memory reads and writes

## Acceptance Criteria

- The default memory system is behind an interface instead of being hardwired into core orchestration.
- A user could add a custom memory backend without editing Prompt Master internals.

