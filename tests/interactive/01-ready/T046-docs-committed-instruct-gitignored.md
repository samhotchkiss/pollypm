# T046: Docs Committed to Git, INSTRUCT.md Gitignored

**Spec:** v1/07-project-history-import
**Area:** Documentation Generation
**Priority:** P1
**Duration:** 10 minutes

## Objective
Verify that generated project documentation is committed to git (available to all developers), but INSTRUCT.md (which contains runtime prompt instructions) is gitignored.

## Prerequisites
- A project has been imported and documentation finalized
- The project is a git repository

## Steps
1. Check the project's `.gitignore` file: `cat .gitignore`. Look for an entry that ignores `INSTRUCT.md` (e.g., `INSTRUCT.md` or `.pollypm/INSTRUCT.md`).
2. Verify INSTRUCT.md exists on disk: `ls -la INSTRUCT.md` or `ls -la .pollypm/INSTRUCT.md`. It should exist locally.
3. Run `git status` and verify INSTRUCT.md does NOT appear in the untracked or staged files (because it is gitignored).
4. Verify the docs directory IS committed: `git log --oneline -- docs/` (or `.pollypm/docs/`) should show commits.
5. Run `git ls-files docs/` and verify documentation files are tracked by git.
6. Verify the committed docs include architecture, timeline, and other generated files.
7. Create a fresh clone of the repository in a temp directory: `git clone <repo> /tmp/test-clone-T046`.
8. Check the clone: `ls /tmp/test-clone-T046/docs/` should show the documentation files.
9. Check the clone for INSTRUCT.md: `ls /tmp/test-clone-T046/INSTRUCT.md` should NOT exist (it was gitignored).
10. Clean up: `rm -rf /tmp/test-clone-T046`.

## Expected Results
- INSTRUCT.md is listed in .gitignore
- INSTRUCT.md exists locally but is not tracked by git
- Documentation files (docs/) are committed and tracked by git
- A fresh clone contains the docs but not INSTRUCT.md
- Generated documentation is available to all project contributors via git

## Log
