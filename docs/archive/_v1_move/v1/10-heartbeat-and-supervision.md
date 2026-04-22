---
## Summary

The heartbeat is PollyPM's liveness and progress authority. It runs as Session 0 — a real interactive CLI agent in tmux that monitors all other sessions on a configurable cadence. It collects signals from tmux panes, transcript JSONL, process state, and git status to classify each session's health, then takes action: nudging idle sessions, recovering exited ones, escalating stuck agents, and managing capacity failover. The heartbeat strategy is a plugin — PollyPM ships opinionated defaults, but the evaluation logic and policy are replaceable. Core owns orchestration, persistence, and effect application; the plugin owns how sessions are evaluated and what actions to recommend.

---

# 10. Heartbeat and Supervision

## Heartbeat Identity

The heartbeat is not a daemon or a cron job. It is a real agent session running in tmux as Session 0. It uses a provider CLI with a system prompt that instructs it to perform monitoring duties. This means:

- It has the same capabilities as any other agent session (file access, shell commands, tool use)
- It can be attached to and observed by the human operator at any time
- It participates in the same tmux session as all other sessions
- If it dies, PollyPM core restarts it before anything else


## Heartbeat Responsibilities

- Check all worker and operator sessions on a configurable cadence
- Record heartbeat snapshots (Level 0 checkpoints)
- Detect stuck, exited, looping, or drifting sessions
- Detect ended turns and idle sessions
- Track account capacity and auth state
- Trigger recovery or failover when needed
- Surface anomalies and alerts to the operator


## Heartbeat Signals

The heartbeat collects these signals for every monitored session on each cycle:

| Signal | Source | What It Reveals |
|--------|--------|-----------------|
| Pane liveness | tmux pane exists, has recent output | Whether the session is still running and producing output |
| Transcript tail | Recent JSONL events from transcript | Last turn type, tool calls, errors, content patterns |
| Process health | Provider CLI process running | Whether the provider CLI has exited or crashed |
| Git state | Uncommitted changes, branch status | Whether the agent is making progress and on the right branch |
| Turn state | Derived from transcript + pane | Active, idle, ended, permission prompt, or error |
| Account capacity | Provider usage endpoints | Whether the account has remaining capacity |


## Health Classification

Each session is classified into exactly one health state on every heartbeat cycle:

| State | Criteria |
|-------|----------|
| active | Agent is mid-turn, producing output. Pane has recent writes, transcript shows ongoing tool calls or text generation. |
| idle | Turn has ended, agent is waiting for input. No recent pane output, transcript shows turn completion. |
| stuck | No progress for N consecutive heartbeat cycles despite appearing active. Pane may have output but transcript shows no meaningful advancement. |
| looping | Repetitive commands or errors detected. Transcript shows the same tool calls, error messages, or patterns repeating. |
| exited | Provider CLI process has exited. Pane exists but shell has returned to prompt, or pane is gone. |
| error | Unrecoverable error state. Provider reported a fatal error, auth failure, or irrecoverable crash. |
| blocked_no_capacity | Account capacity exhausted. Provider usage endpoint reports zero remaining tokens or rate limit with long cooldown. |


## Heartbeat Loop

The heartbeat executes this loop on every cycle:

1. **Enumerate monitored sessions.** Query tmux for all sessions PollyPM is tracking.
2. **Collect signals.** For each session, gather all signals from the table above.
3. **Classify health.** Apply classification rules to determine each session's state.
4. **Handle idle sessions.** Check if there is a next step and the session should be nudged. If the session has pending work, send a nudge to resume.
5. **Handle stuck/looping sessions.** Attempt intervention in escalating order: nudge the agent with a corrective prompt, reset the agent's context, or escalate to the operator.
6. **Handle exited sessions.** Attempt recovery: take a checkpoint, relaunch the provider CLI, inject a recovery prompt with context from the checkpoint.
7. **Handle capacity issues.** Trigger failover to an alternate account or provider as defined in the configuration (doc 02).
8. **Record snapshot.** Write a Level 0 checkpoint capturing the full system state at this heartbeat cycle.
9. **Update TUI state.** Emit events so the TUI dashboard reflects current session health.


## Quick Project State Understanding

The heartbeat needs to rapidly assess what a project's current state is and what should happen next. It does not re-read the entire codebase or replay full history. Instead, it combines four lightweight sources:

1. **Latest checkpoint.** The most recent Level 0 or higher checkpoint gives a snapshot of what the agent was doing and where it left off.
2. **Issue tracker state.** The `next_available` field from the issue tracker (doc 06) tells the heartbeat what work is queued and ready.
3. **project-overview.md.** The project overview (doc 08) provides high-level context about the project's goals, current phase, and priorities.
4. **Last transcript events.** The final N events from the session's transcript JSONL show what the agent most recently did, said, or encountered.

This combination gives the heartbeat enough context to decide: nudge the agent, start new work, escalate to the operator, or leave the session alone.


## Heartbeat Plugin Architecture

The heartbeat is a plugin. The plugin owns evaluation logic and policy — how sessions are classified, what thresholds trigger intervention, and what actions to recommend. Core owns orchestration (running the heartbeat loop), persistence (storing snapshots and state), and effect application (actually sending messages, restarting sessions, etc.).

### Core vs. Plugin Boundary

**Core owns:**
- Running the heartbeat loop on cadence
- Persisting heartbeat snapshots and state under `<project>/.pollypm/`
- Applying effects (sending messages, restarting processes, updating status)
- Providing stable APIs for the plugin to call

**Plugin owns:**
- Session health classification logic
- Intervention policy and escalation thresholds
- Which signals matter and how to weight them

### Stable Core APIs

The heartbeat plugin calls these stable core APIs. Plugins should NOT directly mutate DB tables or depend on internal storage layout.

| API | Purpose |
|-----|---------|
| `list_managed_sessions()` | List all sessions under PollyPM management |
| `get_session_context(session_name)` | Get current context for a session (checkpoint, issue, status) |
| `get_transcript_delta(session_name)` | Get new transcript events since last heartbeat cursor |
| `call_heartbeat_model(...)` | Invoke the heartbeat's backing model for evaluation |
| `send_message(session_name, text)` | Send a message/nudge to a session |
| `raise_alert(...)` | Create a typed alert for operator attention |
| `clear_alert(...)` | Resolve a previously raised alert |
| `set_session_status(...)` | Update a session's health status |
| `queue_followup_for_polly(...)` | Queue a follow-up action for the PM to handle |
| `update_heartbeat_cursor(...)` | Advance the transcript cursor after processing |

### CLI as the v1 Access Layer

In v1, the heartbeat agent accesses core APIs through `pm` CLI commands. The CLI wraps the service API (doc 04), so the heartbeat uses the same stable interface as every other caller. Example commands:

- `pm status` -- check session states
- `pm alert raise` -- create alerts
- `pm session set-status` -- update session health
- `pm heartbeat record` -- write snapshots

CLI-first means the heartbeat works with every provider today -- any agent that can run shell commands can drive PollyPM. Future transports (MCP tools) will give agents richer, structured access to the same APIs without changing the underlying service layer.

### Legacy Backend Interface

The heartbeat also uses a backend interface defined by the provider adapter system (doc 04):

| Method | Purpose |
|--------|---------|
| `enumerate_sessions()` | List all monitored sessions and their tmux identifiers |
| `schedule_cadence()` | Configure the heartbeat interval |
| `collect_signals()` | Gather all signals for a given session |
| `normalize_results()` | Convert provider-specific signal formats to the standard schema |
| `request_recovery()` | Initiate recovery for a failed session |

The default implementation uses local tmux and transcript signals. Future implementations may use API-based or distributed backends.


## Alert Types

The heartbeat emits typed alerts when it detects conditions that require attention:

| Alert | Trigger |
|-------|---------|
| `session_stuck` | Session classified as stuck for N consecutive cycles |
| `session_exited` | Provider CLI process has exited unexpectedly |
| `session_looping` | Repetitive command or error pattern detected |
| `capacity_exhausted` | Account has no remaining capacity and no failover available |
| `auth_broken` | Provider authentication has failed or expired |
| `provider_outage` | Provider API is unreachable or returning errors |
| `drift_detected` | Agent is working on something that does not match its assigned task |
| `progress_stalled` | Agent is active but making no meaningful progress toward its goal |

Alerts are recorded in the event log and surfaced to the operator session (Session 1) and the TUI.


## Intervention Escalation

When the heartbeat detects a problem, it follows an escalation ladder:

1. **Nudge.** Send a short corrective prompt to the session. Effective for idle or mildly stuck sessions.
2. **Reset.** Clear the agent's current context and re-inject from the latest checkpoint. Effective for looping or confused sessions.
3. **Relaunch.** Kill the provider CLI and restart it with a recovery prompt. Effective for exited or error-state sessions.
4. **Failover.** Switch to a different account or provider. Effective for capacity or auth issues.
5. **Escalate.** Alert the operator and wait for human intervention. Used when automated recovery has failed.

Each step is attempted before moving to the next. The heartbeat records which interventions were tried and their outcomes.


## Resolved Decisions

1. **Heartbeat is a real agent session, not a daemon.** Running as Session 0 in tmux gives it full agent capabilities, makes it observable by the operator, and keeps it within the same execution model as everything else.

2. **Transcript JSONL is the primary signal.** The transcript provides the richest and most reliable information about what an agent is doing. Pane output is supplementary.

3. **Level 0 checkpoints on every heartbeat.** Every heartbeat cycle records a snapshot. This creates a dense timeline for debugging and recovery without requiring agents to self-checkpoint.

4. **Quick project state via checkpoint + issue tracker + overview doc.** The heartbeat does not need deep project understanding. These three sources give it enough context to make supervision decisions.

5. **Nudge before escalate.** Automated intervention follows an escalation ladder. The lightest touch is tried first, and human escalation is the last resort.


## Cross-Doc References

- Session roles and Session 0 definition: [01-architecture-and-domain.md](01-architecture-and-domain.md)
- Account failover and capacity management: [02-configuration-accounts-and-isolation.md](02-configuration-accounts-and-isolation.md)
- Provider adapter interface and backend methods: [04-extensibility-and-plugin-system.md](04-extensibility-and-plugin-system.md)
- Issue tracker and next_available: [06-issue-management.md](06-issue-management.md)
- Project overview document: [08-project-state-memory-and-documentation.md](08-project-state-memory-and-documentation.md)
- Inbox and thread routing: [09-inbox-and-threads.md](09-inbox-and-threads.md)
