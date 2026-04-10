# T104: GitHub Issue Backend Replaces File-Based Backend for PollyPM Project

**Spec:** v1/06-issue-management
**Area:** Issue Management — Backend Selection
**Priority:** P0
**Duration:** 15 minutes

## Objective
Verify that the PollyPM project uses the GitHub issue backend instead of the file-based backend, and that all issue operations go through GitHub.

## Prerequisites
- pollypm is running
- The pollypm project config specifies GitHub as the issue backend
- `gh` CLI is authenticated

## Steps
1. Check the project config for issue backend setting
2. Verify `pm` CLI issue commands use GitHub, not file system
3. Create a new issue via the system and verify it appears on GitHub
4. Verify the old issues/ directory is not being used for new issues
5. Check that `next_available()` queries GitHub, not the filesystem
6. Verify `report_status()` shows counts from GitHub labels
7. Check that the cockpit Issues view reads from GitHub
8. Verify that moving an issue updates GitHub labels
9. Test that the file-based issues/ directory (if present) is ignored
10. Verify `gh issue list --label polly:ready` matches what PollyPM shows

## Expected Results
- All issue operations go through GitHub via `gh` CLI
- No new files created in the old issues/ directory
- Cockpit Issues view shows GitHub data
- Status counts match GitHub label counts

## Log
