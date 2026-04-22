---
## Summary

PollyPM manages agent sessions as tmux windows within a single tmux session. Each agent — heartbeat, operator, or worker — is a real interactive CLI process running in its own window. The lease model governs input arbitration between automation and humans. Worker sessions use git worktrees for parallel lanes, and control sessions maintain identity across failover.

---

# 03. Session Management and Tmux

## Session Types

Every session in PollyPM is an interactive CLI agent running in a tmux window. Sessions are declared in `pollypm.toml` and materialized on `pm up`.

| Role | Session # | Count | Purpose |
|------|-----------|-------|---------|
| Heartbeat | 0 | Exactly 1 | Monitors all other sessions, records health snapshots |
| Operator | 1 | Exactly 1 | Project manager, coordinates work, surfaces alerts |
| Worker | 2+ | One per project by default | Implementation and review work on bound projects (multi-session per project is a near-term addition) |

### Key Properties

- Each session is a real interactive CLI agent — not a background daemon, not a headless process
- Sessions are 1:1 with tmux windows
- Worker sessions default to 1:1 with projects (one active worker per project by default; multi-session is a near-term addition with session-scoped state within `<project>/.pollypm/`)
- Sessions persist across PollyPM restarts via tmux session survival
- Session identity (name, role, window position) is stable across failover


## Tmux Integration

### Single Tmux Session

PollyPM creates and manages exactly one tmux session, named `pollypm`. All agent windows live inside this session.

```
tmux session: pollypm
  window 0: heartbeat     (session 0 — heartbeat supervisor)
  window 1: polly         (session 1 — operator)
  window 2: worker-acme   (session 2 — worker for acme project)
  window 3: worker-widgets (session 3 — worker for widgets project)
```

### Window Management

| Operation | Tmux Command | When |
|-----------|-------------|------|
| Create session | `tmux new-session -d -s pollypm` | `pm up` (first time) |
| Create window | `tmux new-window -t pollypm -n <name>` | Session launch |
| Rename window | `tmux rename-window -t pollypm:<idx> <name>` | Session recovery with same identity |
| Kill window | `tmux kill-window -t pollypm:<name>` | Session stop |
| Kill session | `tmux kill-session -t pollypm` | `pm down` |

### Pane Logging

Every session window has pane logging enabled via `tmux pipe-pane`.

```bash
tmux pipe-pane -t pollypm:<name> -o "cat >> <project>/.pollypm/logs/<name>/<launch-id>/pane.log"
```

Pane logging captures all terminal output — agent responses, tool use, errors, and any human input when a human is attached. Logging starts at launch and stops at session termination.

In addition to pane logs, PollyPM maintains its own transcript archive at `<project>/.pollypm/transcripts/` as standardized JSONL. This archive covers every worker session and every Polly conversation about the project. PollyPM does not rely on provider logs — it owns the canonical record.

### Pane Capture

Periodic snapshots of the visible pane content are taken via `tmux capture-pane`.

```bash
tmux capture-pane -t pollypm:<name> -p > <project>/.pollypm/logs/<name>/<launch-id>/snapshots/<timestamp>.txt
```

Captures are taken:

- On the configured `snapshot_interval_s` (default: 60 seconds)
- When the heartbeat detects a state change
- Before any failover or recovery action
- On explicit checkpoint requests

### Sending Input

PollyPM sends prompts and commands to agent sessions via `tmux send-keys`.

```bash
tmux send-keys -t pollypm:<name> "<prompt text>" Enter
```

Input is only sent when PollyPM holds the lease for that pane. If a human holds the lease, input is queued until the lease is released.


## Lease Model

The lease model governs who can send input to a pane at any given time. Only one writer is active per pane.

### Lease Owners

| Owner | Description |
|-------|-------------|
| `pollypm` | Automation holds the lease. PollyPM can send prompts and commands. |
| `human` | A human has taken over. PollyPM queues actions and continues monitoring. |

### Lease Transitions

```
                  ┌──────────────┐
                  │   pollypm    │
                  │  (default)   │
                  └──────┬───────┘
                         │
              human types │ in pane
                         │
                  ┌──────▼───────┐
                  │    human     │
                  │  (claimed)   │
                  └──────┬───────┘
                         │
          30 min idle OR  │ explicit release
                         │
                  ┌──────▼───────┐
                  │   pollypm    │
                  │ (reclaimed)  │
                  └──────────────┘
```

### Lease Rules

1. **Default state is `pollypm`.** When a session launches, automation holds the lease.
2. **Human input auto-claims.** Any keypress detected in the pane while a human is attached transfers the lease to `human`.
3. **Human lease timeout.** The lease returns to `pollypm` after 30 minutes of no human input.
4. **Explicit release.** A human can release the lease via `pm lease release <session>` or a TUI action.
5. **Monitoring continues.** While human-leased, PollyPM still captures pane output, records heartbeats, and classifies health. Only input is blocked.
6. **Actions are queued.** Any automated action (prompt injection, recovery, failover) that requires input is queued while human-leased. Queued actions execute in order when the lease returns.

### Lease State in TUI

The TUI displays lease information for each session:

| Field | Description |
|-------|-------------|
| Owner | Current lease holder (`pollypm` or `human`) |
| Age | Time since lease was acquired |
| Expiry | Time until human lease auto-expires (human leases only) |
| Pending | Number of queued automation actions (human leases only) |

### Human Detection

PollyPM detects human presence by monitoring tmux client attachment and pane input events:

- `tmux list-clients -t pollypm` reveals attached clients
- Pane input that does not originate from `tmux send-keys` (PollyPM's own commands use a tagged prefix) indicates human activity
- The heartbeat checks for human presence on each cycle


## Session Lifecycle

### Launch

Launch is the process of starting a new agent session in a tmux window.

**Steps:**

1. **Resolve account.** Select an account based on the session declaration and current capacity state. Apply failover selection if the declared account is unavailable.
2. **Prepare environment.** Build the environment variables: account-specific HOME, provider config dirs, API keys, project path.
3. **Build launch command.** Construct the provider CLI command with arguments from config. Example: `claude --model opus --system-prompt prompts/worker.md`.
4. **Create tmux window.** `tmux new-window -t pollypm -n <name>`.
5. **Enable pane logging.** `tmux pipe-pane -t pollypm:<name> -o "cat >> <log-path>"`.
6. **Start provider CLI.** `tmux send-keys -t pollypm:<name> "<launch-command>" Enter`.
7. **Register in state store.** Insert a record in `launches` with timestamp, account, provider, and trigger reason.
8. **Set lease to pollypm.** Default lease assignment.
9. **Inject initial prompt.** If the session has a system prompt or recovery context, send it after the CLI is ready.

### Monitor

The heartbeat supervisor monitors all sessions on a continuous loop.

**Per-cycle actions:**

1. **Pane capture.** Take a snapshot of the visible pane content.
2. **Transcript tail.** Read the last N lines of the pane output log.
3. **Health classification.** Ask the provider adapter to classify the session's state:

| State | Meaning |
|-------|---------|
| `idle` | Agent is waiting for input |
| `working` | Agent is actively processing |
| `stuck` | Agent has been in the same state too long |
| `error` | Agent has reported an error |
| `exited` | Agent CLI process has terminated |
| `looping` | Agent is repeating the same action |

4. **State transition.** If health changed, emit an event to the state store.
5. **Alert escalation.** If the state is `stuck`, `error`, `exited`, or `looping`, escalate to the operator.
6. **Heartbeat record.** Write the health check result to the `heartbeats` table.

### Recovery

Recovery restarts a failed session, potentially on the same account.

**Steps:**

1. **Detect failure.** Heartbeat classifies session as `exited`, `stuck`, or `error`.
2. **Checkpoint.** Capture: pane snapshot, git state (branch, status, diff stats), last N transcript lines, operational context.
3. **Select account.** Try the same account first. If unhealthy, apply failover selection.
4. **Kill old window.** `tmux kill-window -t pollypm:<name>` (if it still exists).
5. **Relaunch.** Execute the full launch sequence with the selected account.
6. **Inject recovery prompt.** Construct a prompt containing:
   - What the session was doing (from checkpoint)
   - Current git state (branch, uncommitted changes)
   - Key transcript excerpt (last meaningful output)
   - Instructions to continue from where it left off
7. **Record event.** Log the recovery in the `events` and `launches` tables.

### Failover

Failover is recovery with an account switch. It follows the same steps as recovery, with these differences:

- The account selection explicitly excludes the previous account
- The recovery prompt includes a note that the provider/account has changed
- If the provider changed, the recovery prompt is reformatted for the new provider's conventions
- The `launches` record tags the trigger as `failover` rather than `recovery`

### Stop

Graceful shutdown of a session.

**Steps:**

1. **Final checkpoint.** Save current state for potential future recovery.
2. **Graceful exit.** Send the provider's exit command (e.g., `/exit` for Claude, `Ctrl-C` for Codex).
3. **Wait for exit.** Poll for process termination with a timeout (default: 10 seconds).
4. **Force kill.** If the process hasn't exited, kill the tmux window.
5. **Disable pane logging.** Stop `pipe-pane`.
6. **Update state store.** Mark the session as `stopped` in the `sessions` table.
7. **Release lease.** Clear lease state.


## Worker Sessions and Worktrees

### Git Worktree Model

Worker sessions that target git repositories use dedicated worktrees to enable parallel work.

```
/Users/sam/dev/acme/                    # main worktree
/Users/sam/dev/acme/.pollypm/worktrees/
  worker-acme/                          # worker implementation worktree
  review-acme/                          # review lane worktree (if configured)
```

### Worktree Lifecycle

| Event | Action |
|-------|--------|
| Session launch (first time) | `git worktree add .pollypm/worktrees/<session> -b pollypm/<session>` |
| Session launch (subsequent) | Verify worktree exists, reset if needed |
| Session stop (temporary) | Worktree preserved for recovery |
| Session removal (permanent) | `git worktree remove .pollypm/worktrees/<session>` |
| `pm down` | Worktrees preserved (they survive tmux teardown) |
| `pm clean` | All PollyPM worktrees removed |

### Tracked-Project Mode

For projects under active management, PollyPM supports a two-lane model:

| Lane | Session Role | Worktree | Purpose |
|------|-------------|----------|---------|
| Implementation | Worker (`role = "worker"`) | `.pollypm/worktrees/worker-<project>` | Writing code, running tests, making commits |
| Review | Worker (`role = "worker"`, tagged as reviewer) | `.pollypm/worktrees/review-<project>` | Code review, PR feedback, quality checks |

Both lanes are independent worker sessions with separate accounts. The operator coordinates handoffs between lanes.


## Control Session Behavior

### Identity Stability

Control sessions (heartbeat, operator) maintain their identity across failover:

- **Window name** stays the same (`heartbeat`, `polly`)
- **Window position** stays the same (0, 1)
- **Role prompt** is preserved — the new instance receives the same system prompt
- **Context handoff** — the recovery prompt includes the previous instance's operational context

### Restart Priority

When both control sessions are down:

1. Heartbeat restarts first — monitoring must be operational before coordination
2. Operator restarts second — once heartbeat is running, the operator can receive health updates

If PollyPM core itself restarts:

1. Detect existing tmux session `pollypm`
2. Inventory surviving windows
3. Re-register surviving sessions in state store
4. Restart any missing control sessions
5. Resume monitoring

### Role Prompts

Control sessions receive specialized role prompts that define their behavior:

**Heartbeat prompt includes:**
- List of all sessions to monitor
- Health classification criteria
- Escalation rules
- Snapshot and checkpoint procedures

**Operator prompt includes:**
- Current project statuses
- Worker assignments
- Failover policies
- Communication protocols for interacting with the human


## The `pm` Command Interface

Session management is exposed through the `pm` CLI.

| Command | Description |
|---------|-------------|
| `pm up` | Create tmux session, launch all declared sessions |
| `pm down` | Graceful shutdown of all sessions, kill tmux session |
| `pm status` | Show all sessions with health, account, lease state |
| `pm launch <session>` | Launch a specific session |
| `pm stop <session>` | Graceful stop of a specific session |
| `pm recover <session>` | Force recovery of a specific session |
| `pm failover <session>` | Force failover to a new account |
| `pm lease show` | Show lease state for all sessions |
| `pm lease release <session>` | Release human lease on a session |
| `pm attach <session>` | Attach to a session's tmux window |
| `pm logs <session>` | Tail the pane output log for a session |
| `pm snapshot <session>` | Take an immediate pane snapshot |
| `pm checkpoint <session>` | Save a recovery checkpoint |
| `pm clean` | Remove all PollyPM worktrees and temp files |
| `pm tui` | Launch the Textual dashboard |


## Resolved Decisions

1. **One tmux session, not multiple.** All agent windows live in a single tmux session named `pollypm`. This simplifies window management, client attachment, and session discovery. Multiple tmux sessions would fragment the control plane.

2. **Window-per-agent, not pane-per-agent.** Each agent gets its own tmux window (full screen), not a pane in a split layout. Windows provide clean isolation for pane logging, independent scrollback, and focused human interaction. Pane splits are available for the human to create manually but are not managed by PollyPM.

3. **Lease model for arbitration.** The lease model prevents undefined behavior when both human and automation try to interact with the same agent. Alternatives considered: locking (too rigid, blocks humans), free-for-all (race conditions), separate input channels (not supported by terminal semantics).

4. **30-minute human lease timeout.** Balances human convenience (don't steal back too quickly) with automation needs (don't block forever). The timeout is configurable per session but defaults to 30 minutes. Explicit release is always available.

5. **Worktrees for parallel lanes.** Git worktrees enable multiple sessions to work on the same repository without conflicts. Each worker operates in its own worktree with its own branch. The alternative (stashing/switching branches) was rejected as error-prone and disruptive to in-progress work.


## Cross-Doc References

- Architecture and system roles: [01-architecture-and-domain.md](01-architecture-and-domain.md)
- Account isolation and configuration: [02-configuration-accounts-and-isolation.md](02-configuration-accounts-and-isolation.md)
