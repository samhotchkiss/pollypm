# T042: Import News Project with Git History

**Spec:** v1/07-project-history-import
**Area:** Project Import
**Priority:** P1
**Duration:** 15 minutes

## Objective
Verify that the project history import feature can import an existing project's git history and create a structured project timeline from it.

## Prerequisites
- An existing git repository with meaningful commit history (at least 20 commits)
- Polly is installed and configured
- The target project directory is accessible

## Steps
1. Identify the source repository to import. Note its path and verify it has git history: `git -C <source-repo> log --oneline | head -30`.
2. Run `pm project import <source-repo-path>` (or the equivalent import command).
3. Observe the import process output. It should indicate it is reading git history.
4. Wait for the import to complete. Note how many commits were processed.
5. Verify the project was created: `pm project list` should show the imported project.
6. Check the project's timeline: `pm project timeline <project-name>` or look in the project's docs directory.
7. Verify the timeline includes entries derived from git commits with:
   - Commit dates
   - Commit messages (as timeline descriptions)
   - Author information
8. Verify the timeline entries are in chronological order (oldest first or newest first, as documented).
9. Check that the import created project documentation files in the expected location (e.g., `.pollypm/docs/` or `docs/`).
10. Verify the original repository was not modified by the import process.

## Expected Results
- Import command processes all git commits without errors
- Project appears in `pm project list`
- Timeline reflects the git commit history accurately
- Timeline entries include dates, messages, and authors
- Entries are in chronological order
- Original repository is unchanged
- Project documentation scaffold is created

## Log
