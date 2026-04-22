# Inbox Reply And Thread Model

Maintainer note: this page is an implementation-detail design sketch for inbox
storage, reply threading, and PM/PA routing. It is not front-door product
documentation.

## Goal

Define how inbox items are stored, how replies become threads, and when PollyPM
keeps a conversation inside PM versus routing it to PA or a worker session.

## Scope

- Inbox items are durable records, not ephemeral UI state.
- A reply may stay as a one-off acknowledgement, expand into a PM-led
  discussion, or become a routed worker follow-up.
- Thread state must be recoverable after restart and auditable after the fact.

## File Layout

Use a dedicated inbox tree under the project state area:

```text
state/
  inbox/
    open/
      <message-id>.md
    threads/
      <thread-id>/
        thread.md
        messages/
          0001-operator.md
          0002-pm.md
          0003-pa.md
        state.json
        handoff.json
    closed/
      <thread-id>.md
```

### File Roles

- `state/inbox/open/` holds inbound items that still need an operator decision.
- `state/inbox/threads/<thread-id>/thread.md` is the human-readable thread
  summary and current context.
- `state/inbox/threads/<thread-id>/messages/` holds the ordered reply history.
- `state/inbox/threads/<thread-id>/state.json` stores the canonical thread
  state and timestamps.
- `state/inbox/threads/<thread-id>/handoff.json` records the current owner and
  the next required action when PM and PA exchange responsibility.
- `state/inbox/closed/` holds resolved items with the final outcome and closure
  note.

## State Transitions

Inbox items and their threads move through a small set of explicit states:

1. `open`
2. `threaded`
3. `waiting-on-pa`
4. `waiting-on-pm`
5. `resolved`
6. `closed`

### Transition Rules

- `open -> threaded` when the operator sends a reply that needs persistent
  back-and-forth context.
- `threaded -> waiting-on-pa` when PM has a clear execution request for PA or a
  worker session.
- `threaded -> waiting-on-pm` when PA needs policy, prioritization, or a
  decision that only PM can make.
- `waiting-on-pa -> waiting-on-pm` after PA responds with a question, status
  update, or implementation result that requires PM review.
- `waiting-on-pm -> waiting-on-pa` after PM answers the decision question and
  the thread needs another execution step.
- `resolved -> closed` when the operator confirms the loop is finished or the
  inbox item is no longer actionable.

## PM/PA Routing Rules

PM is the default inbox owner. PA is the execution owner.

- PM handles simple acknowledgements, intake triage, and operator-facing
  summaries.
- PM keeps a thread inside PM when the reply is about priority, policy, scope,
  or whether an inbox item should become work at all.
- PM routes to PA when the operator asks for an action that can be executed
  without another policy decision.
- PM routes to a worker session only when the request belongs to an active
  worker already associated with the thread.
- PA does not directly change inbox state unless it is writing back through the
  thread handoff record.
- A worker reply should be surfaced through PA before it updates the thread
  summary or closes the loop.

### Routing Examples

- `Thanks` or `Acknowledged` stays in PM and may close the thread immediately.
- `Can you do this next?` routes to PA if the ask is execution-only.
- `Should we change the plan?` stays in PM.
- `The worker is blocked on a missing file` routes to PA, then to the owning
  worker session if one exists.
- `Please keep discussing this with me` stays in PM unless the thread
  explicitly asks for worker action.

## Operator UX

- Open inbox items are shown as actionable messages with one clear next step.
- Replying from the inbox either appends to an existing thread or creates a new
  thread record.
- Closing a thread requires an explicit resolution note, even for short
  acknowledgements.
- Reopened items retain the original thread history and append new messages
  instead of overwriting prior state.

## Recovery Rules

- `state.json` is the source of truth for state and ownership.
- `thread.md` is a derived summary and may be regenerated from the message log.
- `handoff.json` lets PM and PA recover the current owner after a restart
  without reading the full conversation first.
- Closed threads stay on disk so audits and follow-up reviews can reconstruct
  the full reply chain.

## Acceptance Criteria

- The inbox tree cleanly separates open items, active threads, and closed
  records.
- Every thread has a single canonical state and a readable conversation
  history.
- PM and PA have non-overlapping default responsibilities.
- A thread can be restored after restart without losing ownership or closure
  status.
