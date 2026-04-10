# T057: Closed Threads Persist in closed/ Directory

**Spec:** v1/09-inbox-and-threads
**Area:** Thread Management
**Priority:** P1
**Duration:** 10 minutes

## Objective
Verify that when a thread is closed, it moves to the closed/ directory and persists there for historical reference, rather than being deleted.

## Prerequisites
- At least one thread exists that can be closed
- Knowledge of the thread directory structure

## Steps
1. Run `pm thread list` and identify an open or resolved thread. Note its ID.
2. Check the thread directory structure: `ls .pollypm/threads/` (or equivalent). Look for `open/` and `closed/` subdirectories.
3. Verify the thread file is in the open/ (or active/) directory: `ls .pollypm/threads/open/<thread-file>`.
4. Close the thread: `pm thread close <thread-id>` or `pm thread transition <thread-id> closed`.
5. Verify the thread is no longer in the open/ directory: `ls .pollypm/threads/open/` — the thread file should be gone.
6. Verify the thread file has moved to the closed/ directory: `ls .pollypm/threads/closed/<thread-file>`.
7. Read the closed thread file and verify all content is preserved (messages, transitions, metadata).
8. Run `pm thread list --status closed` and verify the thread appears in the closed list.
9. Run `pm thread info <thread-id>` and verify the details are still accessible even though the thread is closed.
10. Close a second thread and verify it also moves to closed/ without affecting the first closed thread.

## Expected Results
- Closed threads move from open/ to closed/ directory
- Thread content is fully preserved after closing
- Closed threads are accessible via `pm thread list --status closed` and `pm thread info`
- Multiple closed threads coexist in the closed/ directory
- Threads are never deleted — they persist for historical reference

## Log
