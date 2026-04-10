# T058: Reopen Thread Appends New Messages

**Spec:** v1/09-inbox-and-threads
**Area:** Thread Management
**Priority:** P1
**Duration:** 10 minutes

## Objective
Verify that reopening a closed thread moves it back to the open state and appends new messages without losing the original conversation history.

## Prerequisites
- At least one closed thread exists (from T057 or manually closed)
- Knowledge of the thread's original content

## Steps
1. Run `pm thread list --status closed` and identify a closed thread. Note its ID.
2. Read the thread's current messages: `pm thread messages <thread-id>`. Note the number of messages and the last message content.
3. Reopen the thread: `pm thread reopen <thread-id>` (or `pm thread transition <thread-id> open`).
4. Verify the thread moved back to open status: `pm thread info <thread-id>` should show status "open."
5. Verify the thread file moved from closed/ back to open/: `ls .pollypm/threads/open/<thread-file>`.
6. Read the thread messages again: `pm thread messages <thread-id>`. Verify ALL original messages are still present.
7. Add a new message to the reopened thread: `pm thread message <thread-id> "Reopened for additional follow-up. Please re-examine the previous resolution."`.
8. Read the messages again and verify the new message was appended AFTER the existing messages.
9. Verify the original messages were not modified (same content, same timestamps).
10. Verify the reopen event appears in the thread's transition history with a timestamp.

## Expected Results
- Closed thread can be reopened successfully
- Thread moves from closed/ back to open/ directory
- All original messages are preserved after reopening
- New messages are appended to the existing conversation
- Original messages retain their original timestamps and content
- Reopen event is recorded in the transition history

## Log
