# T019: Git Worktree Created for Each Worker Session

**Spec:** v1/03-session-management-and-tmux
**Area:** Worker Isolation
**Priority:** P0
**Duration:** 10 minutes

## Objective
Verify that each worker session operates in its own git worktree, preventing file conflicts between concurrent workers.

## Prerequisites
- `pm up` has been run with at least two worker sessions (or configure for two workers before starting)
- The project is a git repository

## Steps
1. Run `pm status` and identify all active worker sessions (e.g., worker-0, worker-1).
2. Attach to worker-0 via `pm console worker-0`.
3. Inside the worker-0 session, check the current working directory: observe or run `pwd` if possible. Note the path — it should be a worktree path (e.g., `.pollypm/worktrees/worker-0/` or similar).
4. Detach and attach to worker-1 via `pm console worker-1`.
5. Check the working directory for worker-1. It should be a DIFFERENT worktree path (e.g., `.pollypm/worktrees/worker-1/`).
6. Detach and verify the worktrees exist on disk: `ls -la .pollypm/worktrees/` (or the configured worktree location).
7. Run `git worktree list` from the main repository. Verify that each worker's worktree is listed.
8. Verify the worktrees are on different branches or at least have independent working trees: `git -C <worker-0-worktree> status` and `git -C <worker-1-worktree> status`.
9. Create a test file in worker-0's worktree: `touch <worker-0-worktree>/test-isolation.txt`.
10. Verify the file does NOT exist in worker-1's worktree: `ls <worker-1-worktree>/test-isolation.txt` should fail. Clean up: `rm <worker-0-worktree>/test-isolation.txt`.

## Expected Results
- Each worker session has its own distinct git worktree
- Worktrees are listed in `git worktree list`
- Working directories are different for each worker
- File changes in one worktree do not appear in another
- Workers are fully isolated from each other's file operations

## Log
