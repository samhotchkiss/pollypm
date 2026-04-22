---
## Summary

PollyPM is a tmux-first supervisor for interactive CLI agent sessions. It orchestrates multiple Claude, Codex, and future CLI agents from a single control plane without replacing native tools. The system treats tmux as the shared cockpit where human takeover is always possible, and PollyPM as the supervisor and state authority that manages session lifecycle, health monitoring, failover, and recovery across providers and accounts.

---

# 01. Architecture and Domain

## Core Principles

These principles are non-negotiable and override any implementation convenience.

| # | Principle | Implication |
|---|-----------|-------------|
| 0 | Opinionated but pluggable | Every system ships strong opinionated defaults, but everything is replaceable. When designing any feature, the first question is "what's our best opinion?" and the second is "how does someone swap this out?" This is the foundational design philosophy — it governs defaults, configuration, plugin architecture, and agent behavior. |
| 1 | Tmux is the shared cockpit — human takeover always possible | Every agent runs in a real tmux window. A human can attach, type, and take over at any time. PollyPM never hides the session. |
| 2 | PollyPM is supervisor and state authority | PollyPM owns session lifecycle, health classification, failover decisions, and the event log. Providers do not self-supervise. |
| 3 | Providers are adapters, not the center | Claude, Codex, Aider, Gemini, OpenCode are interchangeable execution backends. PollyPM abstracts their differences behind a provider adapter interface. |
| 4 | Runtime isolation is separate from provider behavior | Account home directories, environment variables, and filesystem boundaries are PollyPM's responsibility — not the provider's. |
| 5 | One active worker per project by default | A project defaults to one worker session doing implementation work at any moment. Review lanes are separate sessions with separate worktrees. Note: multi-session per project is a near-term addition — the architecture supports multiple concurrent sessions with session-scoped state, paths, and locks within `<project>/.pollypm/`. |
| 6 | The agent is the configuration interface | When a user disagrees with behavior, the agent asks if they want to change it and patches the relevant override file. Users should not need to hand-edit config files — the agent mediates configuration changes conversationally. |


## System Roles

PollyPM organizes sessions into three roles with fixed session numbering.

### Session 0: Heartbeat Supervisor

The heartbeat supervisor is the lowest-level watchdog. It runs as session 0 and performs:

- Periodic health checks on all worker and operator sessions
- Detection of stuck, exited, looping, and crashed states
- Pane snapshot recording at configurable intervals
- Event emission for state transitions
- Self-recovery: if the heartbeat dies, PollyPM core restarts it before anything else

The heartbeat supervisor is an agent session itself — it runs a provider CLI with a system prompt that instructs it to perform monitoring duties.

### Session 1: Operator Interface (Polly)

The operator is the project manager. It runs as session 1 and handles:

- Surfacing alerts from the heartbeat supervisor
- Coordinating work across worker sessions
- Making scheduling and prioritization decisions
- Communicating status to the human via the TUI and tmux
- Initiating failover and recovery sequences when the heartbeat detects problems

The operator receives structured context about system state and acts on it through PollyPM's command interface.

### Sessions 2+: Worker Sessions

Worker sessions do the actual implementation and review work. Each worker session:

- Is bound to exactly one project
- Runs exactly one provider CLI (Claude, Codex, etc.)
- Uses a specific account with isolated credentials and home directory
- Operates in the project's directory or a dedicated git worktree
- Is monitored by the heartbeat supervisor


## Core Components

### PollyPM Core

The central orchestration layer. Responsibilities:

- **Config loading**: Parse `pollypm.toml`, validate session/account/project declarations
- **Account management**: Track account state, capacity, auth health, cooldown timers
- **Session management**: Launch, monitor, recover, failover, and stop agent sessions
- **Tmux bootstrap**: Create the tmux session, allocate windows, configure pane logging
- **Tmux/session/process primitives**: Own the runtime substrate — tmux windows, pane access, process lifecycle
- **Transcript and pane access**: Provide transcript ingestion and pane capture to all callers
- **LLM account/session plumbing**: Manage credentials, session routing, and provider connections
- **Scheduling/cron trigger**: Drive recurring and one-shot orchestration jobs
- **State store**: Record all events, heartbeats, launches, and alerts to SQLite (durable state store)
- **Stable internal API**: Expose a defined API surface that plugins call; plugins never reach into core internals
- **CLI and TUI**: Expose `pm` commands via Typer and a Textual-based dashboard

### Provider Adapters

Each supported CLI agent gets a provider adapter that implements:

- **Launch command construction**: Build the shell command to start the provider CLI
- **Environment preparation**: Set provider-specific env vars (API keys, config dirs, home paths)
- **Health signal parsing**: Interpret pane output to detect idle, working, stuck, error, exited states
- **Prompt injection**: Format and deliver prompts/instructions to the running agent
- **Capacity querying**: Check usage limits, token balances, rate-limit status

Supported providers:

| Provider | CLI | Status |
|----------|-----|--------|
| Claude | `claude` | Supported |
| Codex | `codex` | Supported |
| Aider | `aider` | Future |
| Gemini CLI | `gemini` | Future |
| OpenCode | `opencode` | Future |

### Runtime Adapters

Runtime adapters handle execution environment isolation:

- **Local runtime**: Default. Each account gets a home directory under `~/.pollypm/homes/`. Provider-native env vars point to account-specific paths. No containerization.
- **Docker runtime** (future): Run each worker in a container with mounted project directories and isolated filesystems.

Runtime adapters are orthogonal to provider adapters. Any provider can run in any runtime.

### Tmux Layer

The tmux layer manages the execution surface:

- **Window management**: Create, name, reorder, and destroy tmux windows
- **Pane logging**: `tmux pipe-pane` streams all pane output to log files
- **Pane capture**: `tmux capture-pane` takes point-in-time snapshots for health analysis
- **Lease model**: Tracks whether automation or a human currently owns input to each pane
- **Session lifecycle**: The single tmux session is created on `pm up` and torn down on `pm down`

### State Store

SQLite database at `<project>/.pollypm/state/pollypm.db` (project-scoped) with these core tables:

| Table | Purpose |
|-------|---------|
| `sessions` | Declared sessions, their roles, providers, accounts, and current state |
| `launches` | Every launch/relaunch of a session, with timestamps and trigger reason |
| `events` | State transitions, health changes, failover triggers, human interactions |
| `heartbeats` | Periodic health check results per session |
| `alerts` | Unresolved alerts surfaced to the operator |
| `checkpoints` | Recovery data: git state, transcript excerpts, operational context |


## Logging Architecture

Each worker session produces three log streams:

1. **Pane output log**: Raw terminal output captured via `tmux pipe-pane`. Contains everything the agent prints. Stored at `<project>/.pollypm/logs/<session>/<launch-id>/pane.log`.

2. **Supervisor action log**: PollyPM's own actions — prompts sent, health classifications made, failover decisions, lease changes. Stored at `<project>/.pollypm/logs/<session>/<launch-id>/supervisor.log`.

3. **Periodic snapshots**: Point-in-time captures of visible pane content via `tmux capture-pane`. Stored as timestamped files in `<project>/.pollypm/logs/<session>/<launch-id>/snapshots/`.

4. **Transcript archive**: Standardized JSONL in `<project>/.pollypm/transcripts/` covering every worker session and every Polly conversation about the project. PollyPM owns its own transcript archive — it does not rely on provider logs as the source of truth. Provider JSONL is ingested and normalized into this archive.

Control sessions (heartbeat, operator) produce the same three streams.


## Recovery Model

Recovery rebuilds working state from durable artifacts. PollyPM does not attempt to restore exact provider internal state — that is impossible and unnecessary.

Recovery inputs:

| Source | What it provides |
|--------|-----------------|
| Git state | Current branch, commit history, uncommitted changes, worktree status |
| Session definition | Provider, account, project binding, role, args |
| Transcripts | PollyPM's own transcript archive (`<project>/.pollypm/transcripts/`) — standardized JSONL covering what the agent said and did |
| Checkpoints | Explicit recovery data saved at key moments (pre-failover, pre-stop) |
| Operational events | State store records of what happened and why |

Recovery procedure:

1. Read the session definition and checkpoint data
2. Select an available account (same or different provider)
3. Launch a fresh provider CLI in the session's tmux window
4. Inject a recovery prompt containing: task context, git state summary, transcript excerpt, and instructions to continue

The recovery prompt is the bridge between the old session and the new one. It must be provider-aware (each adapter formats it differently) and concise enough to fit in the provider's context window.


## Technology Stack

| Component | Technology | Rationale |
|-----------|-----------|-----------|
| Language | Python 3.13+ | Ecosystem support, rapid iteration, Textual/Typer compatibility |
| TUI | Textual | Rich terminal UI, async-native, good tmux coexistence |
| CLI | Typer | Clean CLI framework, integrates with Textual |
| State store | SQLite | Zero-config, single-file, sufficient for local supervisor workloads |
| Execution surface | tmux | Universal, scriptable, human-attachable terminal multiplexer |
| Package management | uv | Fast Python package installer, lockfile support |
| Config format | TOML | Python-native, readable, hierarchical |


## Boundaries and Non-Goals

PollyPM is not:

- A replacement for Claude, Codex, or any provider CLI
- A CI/CD system or build tool
- A multi-machine orchestrator (single-host only)
- A provider API wrapper (it drives interactive CLIs, not APIs)
- A prompt engineering framework

PollyPM does not:

- Modify provider CLI source code or behavior
- Intercept or proxy API calls between providers and their backends
- Manage cloud infrastructure or containers (Docker runtime is future and optional)
- Handle billing, metering, or cost optimization beyond capacity tracking


## Resolved Decisions

1. **Tmux as execution surface.** Every agent session is a real tmux window. This gives humans direct access, enables pane logging and capture, and avoids reinventing terminal multiplexing. The alternative (pty management in Python) was rejected for complexity and fragility.

2. **SQLite, not Postgres.** PollyPM is a single-host local supervisor. SQLite is zero-config, embedded, and more than sufficient. Postgres adds deployment complexity with no benefit for this workload.

3. **Python, not Go.** The ecosystem (Textual, Typer, rich async support, rapid prototyping) outweighs Go's performance advantages. PollyPM is I/O-bound, not CPU-bound.

4. **Modular monolith.** Single Python package with clear internal module boundaries (core, providers, runtimes, tmux, state, tui, cli). No microservices, no separate processes beyond the tmux sessions themselves.

5. **One active worker per project by default.** The default is one worker to prevent conflicts from multiple agents editing the same codebase simultaneously. Parallel work on the same project uses separate worktrees with separate sessions. Multi-session per project is a near-term addition — the architecture supports it with session-scoped state and locks. The universal rules (default to action, prove it works, etc.) are opinionated defaults that CAN be overridden per-project via `<project>/.pollypm/config/` — they are strong opinions, not sacred cows. Per the foundational "opinionated but pluggable" philosophy, every default is replaceable.

6. **Lease model for human/automation arbitration.** Rather than locking humans out or having undefined behavior when both human and automation try to type, the lease model makes ownership explicit and gives humans automatic priority.


## Cross-Doc References

- Account isolation and configuration model: [02-configuration-accounts-and-isolation.md](02-configuration-accounts-and-isolation.md)
- Session lifecycle and tmux integration: [03-session-management-and-tmux.md](03-session-management-and-tmux.md)
