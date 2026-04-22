---
## Summary

PollyPM's inbox is a file-based message queue where inbound requests, replies, and alerts land for operator triage. Items flow through explicit state transitions from open to closed, threads accumulate conversation history, and routing rules determine whether the PM, PA, or a worker handles each message. The PM is the default inbox owner; the PA only touches inbox state through handoff records. The inbox system is a plugin — PollyPM ships opinionated defaults, but users can replace the inbox implementation entirely.

---

# 09. Inbox and Threads

## File Layout

All inbox state lives under `<project>/.pollypm/inbox/` in the project directory.

```
<project>/.pollypm/inbox/
  open/                          # Items needing operator decision
    <item-id>.md                 # One file per inbound item
  threads/<thread-id>/           # Active conversations
    thread.md                    # Human-readable thread summary (derived)
    messages/                    # Individual messages in order
      001-<timestamp>.md
      002-<timestamp>.md
    state.json                   # Source of truth for thread state
    handoff.json                 # Ownership tracking and recovery
  closed/                        # Resolved items (kept for audit)
    <thread-id>/                 # Same structure as threads/
```

### Pluggability

The inbox system is a plugin. PollyPM ships with a strong default file-based inbox, but the entire inbox implementation can be replaced. Custom inbox plugins must conform to the inbox interface and handle the same state transitions, but they are free to use different storage backends, routing logic, or notification mechanisms. This follows PollyPM's "opinionated but pluggable" philosophy: strong defaults, replaceable parts.

### File Responsibilities

- **open/<item-id>.md**: Raw inbound item. Contains the message body, sender context, timestamp, and any metadata. One file per item, named with a sortable unique ID.
- **thread.md**: Derived summary of the conversation. Regenerated from messages/ whenever the thread is updated. Not the source of truth.
- **messages/**: Ordered message files. Each message records sender (operator, PM, PA, worker), timestamp, and body. Messages are append-only.
- **state.json**: Canonical thread state. Records current status, owner, creation time, last update, transition history, and any pending actions.
- **handoff.json**: Records ownership transfers between PM, PA, and workers. Used for recovery when ownership is ambiguous after a crash or restart.


## State Transitions

Every inbox item follows a linear progression through these states:

```
open → threaded → waiting-on-pa → waiting-on-pm → resolved → closed
                  ↕               ↕
                  (can cycle between waiting states)
```

| State | Meaning |
|-------|---------|
| open | New item, not yet triaged. Sitting in `open/` directory. |
| threaded | PM has acknowledged the item and created a thread. Moved to `threads/`. |
| waiting-on-pa | PM has routed execution to PA. PA owns next action. |
| waiting-on-pm | PA or worker has responded. PM owns next action. |
| resolved | All parties agree the item is done. Awaiting close. |
| closed | Thread moved to `closed/`. No further action expected. |

Transitions are recorded in `state.json` with timestamps and the actor that triggered the transition. No transition happens without an explicit write to `state.json`.


## PM/PA Routing Rules

### Ownership Defaults

- **PM is the default inbox owner.** Every item lands in the PM's domain first. The PM triages, acknowledges, and decides disposition.
- **PA is the execution owner.** When work needs to happen, the PM routes to the PA. The PA coordinates with workers.

### What the PM Handles

The PM keeps ownership when the message involves:

- **Acknowledgements**: confirming receipt, thanking the sender, noting the request
- **Triage**: classifying priority, categorizing the request, deciding next steps
- **Summaries**: synthesizing thread state for the operator
- **Policy decisions**: anything involving scope, priorities, tradeoffs, or plan changes

### When PM Keeps the Thread

The PM does not route away when:

- The message is a priority or policy question
- The message asks about scope or plan changes
- The message requires operator judgment before any execution can start
- The thread has unresolved ambiguity that only the operator or PM can resolve

### When PM Routes to PA

The PM routes to PA when:

- The request is execution-only and requires no policy decision
- The task is well-defined and can be completed without further operator input
- The PM has already made the relevant policy decisions and only execution remains

### When PM Routes to Worker

The PM routes to a worker (through PA) when:

- The request belongs to an active worker's current task
- The message contains information the worker needs to unblock

### PA Constraints

- The PA does not change inbox state directly. All inbox state changes go through handoff records.
- The PA writes to `handoff.json` to record that it has taken or returned ownership.
- Worker replies surface through the PA before the thread is updated. The PA adds worker responses to the thread's messages/ directory and updates `handoff.json`.


## Routing Examples

| Inbound Message | Routing | Reason |
|-----------------|---------|--------|
| "Thanks" | Stays in PM, may close thread | Acknowledgement, no execution needed |
| "Can you do this?" | Routes to PA if execution-only | Well-defined task, no policy decision |
| "Should we change the plan?" | Stays in PM | Policy/scope question requiring operator judgment |
| "Worker blocked on file" | Routes to PA, then PA routes to worker | Execution blocker belonging to active worker task |
| "What's the status?" | Stays in PM | Summary request, PM synthesizes from thread state |
| "Deploy this to staging" | Routes to PA | Execution task with no policy ambiguity |


## Thread Lifecycle

1. **Item arrives** in `open/`. A new `.md` file is created with the message content and metadata.
2. **PM triages**. PM reads the item, creates a thread directory under `threads/`, moves the item content into `messages/001-<timestamp>.md`, initializes `state.json` with status `threaded`, and generates `thread.md`.
3. **Routing**. PM decides whether to keep the thread, route to PA, or route to worker. If routing, PM updates `state.json` and writes `handoff.json`.
4. **Conversation**. Messages accumulate in `messages/`. Each reply appends a new numbered file. `state.json` is updated on every transition. `thread.md` is regenerated.
5. **Resolution**. When the thread's purpose is fulfilled, state transitions to `resolved`. PM confirms and transitions to `closed`.
6. **Archival**. The entire thread directory is moved from `threads/` to `closed/`. No files are deleted.


## Recovery

Recovery relies on the file-based state being self-describing:

- **state.json is the source of truth.** If `thread.md` is corrupt or missing, it can be regenerated from `messages/`. If `state.json` is present, the thread's status and history are recoverable.
- **handoff.json recovers ownership.** After a crash, the system reads `handoff.json` to determine who last owned the thread and what action was pending. If `handoff.json` is missing but `state.json` is present, ownership defaults back to PM.
- **messages/ is append-only.** Message files are never modified after creation. This makes the conversation history resilient to partial writes.
- **closed/ is the audit trail.** Closed threads are never deleted. They provide a full history of every inbox interaction for debugging and review.


## Resolved Decisions

1. **File-based inbox.** Inbox state is stored as files on disk, not in a database. This makes state inspectable with standard tools, easy to back up, and consistent with PollyPM's file-first philosophy.

2. **PM is default owner.** Every inbox item starts with PM ownership. This prevents items from falling through the cracks and ensures operator-level triage happens before any execution.

3. **Explicit state transitions.** Thread state changes only through writes to `state.json` with recorded timestamps and actors. No implicit transitions.

4. **handoff.json for ownership recovery.** A dedicated file tracks ownership transfers so that crash recovery can determine who was responsible for the next action.

5. **Closed threads persist.** Resolved threads are moved to `closed/` but never deleted. This provides a complete audit trail of all inbox activity.


## Cross-Doc References

- Session roles and numbering (PM, PA, workers): [01-architecture-and-domain.md](01-architecture-and-domain.md)
- Heartbeat monitoring of session health: [10-heartbeat-and-supervision.md](10-heartbeat-and-supervision.md)
- Agent personas and prompt system: [11-agent-personas-and-prompt-system.md](11-agent-personas-and-prompt-system.md)
- Provider adapter interface: [04-extensibility-and-plugin-system.md](04-extensibility-and-plugin-system.md)
