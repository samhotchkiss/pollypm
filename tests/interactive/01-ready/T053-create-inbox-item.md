# T053: Create Inbox Item and Verify It Appears in open/

**Spec:** v1/09-inbox-and-threads
**Area:** Inbox Management
**Priority:** P0
**Duration:** 10 minutes

## Objective
Verify that creating an inbox item places it in the open/ directory (or equivalent "open" state) and that it is visible for triage by the PM.

## Prerequisites
- `pm up` has been run (or at least the inbox system is functional)
- The inbox directory structure exists (`.pollypm/inbox/open/`, `.pollypm/inbox/closed/`)

## Steps
1. Check the current inbox: `pm inbox list` and note any existing items.
2. Verify the inbox directory structure: `ls .pollypm/inbox/`. It should have `open/` and `closed/` subdirectories.
3. List open inbox items: `ls .pollypm/inbox/open/`. Note the count.
4. Create a new inbox item: `pm inbox create --title "Test inbox item" --body "This is a test item for T053. Please triage this into a thread."`.
5. Note the returned item ID.
6. Verify the item appears in `pm inbox list` with status "open" or "new."
7. Verify the item file exists on disk: `ls .pollypm/inbox/open/`. The new item file should be present.
8. Read the item file: `cat .pollypm/inbox/open/<item-file>`. Verify it contains the title, body, creation timestamp, and status.
9. Run `pm inbox info <item-id>` and verify the details match.
10. Verify the item is NOT in the closed/ directory: `ls .pollypm/inbox/closed/` should not contain the item.

## Expected Results
- Inbox item is created successfully with a unique ID
- Item file is placed in the `open/` directory
- Item contains all required metadata (title, body, timestamp, status)
- `pm inbox list` shows the item
- Item is not in the `closed/` directory
- Item is available for triage by the PM

## Log
