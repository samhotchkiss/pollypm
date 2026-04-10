# T043: Timeline Built from Git Commits Chronologically

**Spec:** v1/07-project-history-import
**Area:** Project Import
**Priority:** P1
**Duration:** 10 minutes

## Objective
Verify that the project timeline is constructed from git commits in correct chronological order, including merge commits, branch points, and all relevant metadata.

## Prerequisites
- A project has been imported with git history (T042 completed or equivalent)
- The imported project has a non-trivial git history (branches, merges, multiple authors)

## Steps
1. View the project timeline: `pm project timeline <project-name>` or read the timeline file directly.
2. Identify the first entry in the timeline. It should correspond to the earliest (or latest, depending on sort order) commit in the git history.
3. Cross-reference with git: `git -C <source-repo> log --reverse --oneline | head -5` and verify the first few timeline entries match.
4. Check the last entry in the timeline: `git -C <source-repo> log --oneline | head -1` and verify it matches.
5. Verify timestamps are in strict chronological order. No entry should have a timestamp earlier than the previous entry (when sorted oldest-first).
6. Look for merge commits in the timeline. Verify they are included and indicate they are merges.
7. Verify that commits from different branches are included and properly ordered by timestamp.
8. Check that multiple authors are represented: the timeline should show different author names for different entries.
9. Verify that commit messages are accurately captured (no truncation or corruption).
10. Count the timeline entries and compare with `git -C <source-repo> rev-list --count HEAD` — they should be equal or close (some filtering may apply).

## Expected Results
- Timeline entries match git commit history
- Chronological order is correct (timestamps are monotonically increasing or decreasing)
- Merge commits are included
- Multiple authors are represented
- Commit messages are accurately captured
- Entry count matches the expected number of commits

## Log
