# 0012 Pluggable Task Backend

## Goal

Extract the file-based issue tracker behind a replaceable task backend contract.

## Scope

- default file-backed tracker adapter
- backend interface for listing, moving, annotating, and creating tasks
- room for GitHub Issues / Linear / Jira adapters later

## Acceptance Criteria

- Prompt Master core depends on a task backend interface instead of folder paths directly.
- The current issue tracker continues to work as the default backend.

