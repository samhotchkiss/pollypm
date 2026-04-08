# 0007 Inbox Management And Reply Flow

## Goal

Define how PollyPM inbox messages behave when the operator replies, how follow-up conversation threads are handled, and how those threads connect back to PM/PA sessions.

## Completed

- Added a concrete inbox/thread design in [`docs/inbox-reply-thread-model.md`](docs/inbox-reply-thread-model.md).
- Specified the on-disk file layout for open inbox items, active threads, handoff state, and closed records.
- Defined explicit thread states and transitions from open intake through closure.
- Clarified PM/PA routing rules for acknowledgements, decisions, execution requests, and worker follow-ups.

## Validation

- The design separates inbox storage from live PM/PA session state.
- The thread model supports auditability, recovery, and repeated back-and-forth without losing history.
