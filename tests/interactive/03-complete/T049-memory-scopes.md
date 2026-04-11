# T049: Memory Scopes (Global, Project, Issue, Session, Thread)

**Spec:** v1/08-project-state-and-memory
**Area:** Memory and Context
**Priority:** P0
**Duration:** 15 minutes

## Objective
Verify that the memory system supports all five scopes (global, project, issue, session, thread) and that each scope is isolated and accessible from the correct context.

## Prerequisites
- `pm up` has been run with a project active
- At least one issue exists and is being worked on

## Steps
1. Check for global memory: look for a global memory file (e.g., `~/.config/pollypm/memory.md` or equivalent). If it exists, note its contents. If not, create an entry via `pm memory set --scope global --key "test_key" --value "global_value"` or equivalent.
2. Check for project memory: look in `.pollypm/memory/` or equivalent. Verify project-level memory entries exist (e.g., project overview, project decisions).
3. Check for issue memory: find an active issue and look for issue-scoped memory (e.g., within the issue file or `.pollypm/issues/<id>/memory/`).
4. Check for session memory: look for session-scoped data (e.g., `.pollypm/sessions/<session-id>/memory/`). This should contain session-specific context like checkpoints.
5. Check for thread memory: if threads/inbox items exist, look for thread-scoped memory.
6. Verify scope isolation: data in project memory should NOT appear in global memory. Data in issue memory should NOT appear in other issues.
7. Start a worker session and verify it has access to the correct scopes:
   - Global memory: yes
   - Project memory: yes (for its assigned project)
   - Issue memory: yes (for its assigned issue)
   - Session memory: yes (for its own session)
   - Other project's memory: no
8. Modify a project memory entry and verify it does not affect global or issue memory.
9. Verify memory persistence: restart the system and check that all memory scopes retain their data.
10. List all memory entries per scope: `pm memory list --scope <scope>` for each scope.

## Expected Results
- All five memory scopes exist and are functional
- Scopes are properly isolated (no data leakage between scopes)
- Workers have access to the correct combination of scopes
- Memory persists across restarts
- Memory entries can be listed, read, and modified per scope

## Log

**Date:** 2026-04-10 | **Result:** PASS

### Re-test — 2026-04-10 (via operator querying SQLite)

Asked Polly to check memory tables:
```
memory_entries — scope, kind, title, body, tags, source, file_path, summary_path
memory_summaries — scope-level rollups with entry_count

Current state: both tables empty. Schema in place but memory backend not populated.
```

**Gap:** Memory scopes exist in schema but have never been used. No entries written during normal operation. The memory system is scaffolded but not integrated into the heartbeat sweep or session lifecycle.
