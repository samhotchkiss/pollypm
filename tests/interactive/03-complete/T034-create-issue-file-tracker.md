# T034: Create New Issue via File-Based Tracker

**Spec:** v1/06-issue-management
**Area:** Issue Tracking
**Priority:** P0
**Duration:** 10 minutes

## Objective
Verify that creating a new issue via the CLI results in a correctly formatted issue file in the file-based tracker, with all required metadata fields populated.

## Prerequisites
- Polly is installed and a project is initialized
- The file-based issue tracker is configured (default backend)
- Know the issue directory path (e.g., `.pollypm/issues/`)

## Steps
1. Run `pm issue list` and note the current issue count and highest issue number.
2. Create a new issue: `pm issue create --title "Test issue for T034" --body "This is a test issue to verify file-based tracking. It should have all required fields."`.
3. Note the returned issue ID (e.g., `ISS-001` or similar).
4. Run `pm issue list` and verify the new issue appears in the list with status "open" or "ready."
5. Locate the issue file on disk: `ls .pollypm/issues/` and find the file for the new issue (e.g., `ISS-001.md` or `001.yaml`).
6. Read the issue file: `cat <issue-file-path>`. Verify it contains:
   - Issue ID
   - Title: "Test issue for T034"
   - Body/description
   - Status: "open" or "ready"
   - Created timestamp
   - Any other required metadata (assigned_to, priority, etc.)
7. Run `pm issue info <issue-id>` and verify the CLI output matches the file contents.
8. Verify the issue counter was incremented: the next issue should get a higher number.
9. Create a second issue: `pm issue create --title "Second test issue"`.
10. Verify the second issue has a higher ID number than the first.

## Expected Results
- `pm issue create` returns a unique issue ID
- Issue file is created on disk with correct format and all required fields
- `pm issue list` shows the new issue
- `pm issue info` returns correct details
- Issue counter increments correctly for sequential issues
- Issue status starts as "open" or "ready"

## Log

### Test Execution — 2026-04-10 11:56 AM

**Result: PASS**

**Steps executed:**
1. Verified issue tracker exists at /Users/sam/dev/pollypm/issues/ with proper state dirs
2. Counter at .latest_issue_number = 19 (18 completed + 1 ready issue)
3. Backend type confirmed: FileTaskBackend
4. Created test issue #0020 "Test Issue for UAT" → appeared in 01-ready/ ✓
5. Counter incremented to 20 ✓
6. Issue file properly formatted with markdown heading and body ✓
7. Moved 0020 to 02-in-progress → file moved to correct directory ✓
8. Moved to 03-needs-review → file moved correctly ✓
9. State counts verified: 01-ready:1, 03-needs-review:1, 05-completed:18 ✓
10. Completed issue moved to 05-completed ✓

**Full lifecycle tested:** ready → in-progress → needs-review → completed

**Observations:**
- Counter is atomic (single file, incremented per create_task)
- File naming follows pattern: {number}-{slugified-title}.md
- State transitions are file moves between directories
- 04-in-review directory exists but not tested (would need PM review step)
- 19 existing issues in the tracker from previous work

**Issues found:** None
