# PollyPM Architecture

## Goal

PollyPM orchestrates multiple interactive CLI agent sessions from one control plane without replacing the native tools. Every worker remains a real `claude`, `codex`, or future CLI session inside tmux. PollyPM supervises those sessions, logs them, and assists the operator.

## Non-goals for MVP

- exact hidden-context restoration after crashes
- full autonomous project-management judgment
- provider-specific deep integrations beyond launch and monitoring
- perfect quota accounting for subscription plans

## Core principles

1. Tmux is the shared cockpit.
2. PollyPM is the supervisor and state authority.
3. Providers are adapters, not the center of the design.
4. Runtime isolation is a separate concern from provider behavior.
5. Human takeover must always be possible.

## Roles

### Session 0: Heartbeat supervisor

- checks all known worker sessions on a timer
- records a heartbeat snapshot
- detects likely stuck, exited, or looping sessions
- keeps launch and recovery metadata current
- runs as a true interactive provider session in tmux

### Session 1: Operator interface

- surfaces alerts and recommended actions
- lets the user inspect or control sessions
- acts as the project-manager role when enabled
- runs as a true interactive provider session in tmux

### Sessions 2+

- actual worker sessions
- each session runs one provider in one tmux window
- each session belongs to an account profile and runtime profile

## System components

### 1. PollyPM core

Responsibilities:

- load config
- resolve accounts, runtimes, and sessions
- bootstrap tmux windows
- register sessions in the state store
- provide control and status commands

### 2. Provider adapters

Each provider adapter implements:

- binary name and availability check
- command construction for an interactive launch
- provider-specific default flags
- basic health pattern hooks

Initial adapters:

- `claude`
- `codex`

Future adapters:

- `aider`
- `gemini-cli`
- `opencode`
- any terminal-native agent CLI

### 3. Runtime adapters

Runtime adapters isolate session execution from the provider.

Initial runtime:

- `local`: launches the provider in the host environment with isolated per-account homes and provider-native profile variables such as `CLAUDE_CONFIG_DIR` and `CODEX_HOME`

Planned runtime:

- `docker`: one container or long-lived runtime per account profile, with isolated auth, cache, and config state

### 4. Tmux layer

Tmux is the operator-facing execution surface.

PollyPM uses tmux for:

- window creation
- pane output logging
- session listing
- capture snapshots
- human handoff and direct interaction

Operational rule:

- PollyPM operational commands should run only from inside the `promptmaster` tmux session
- startup is the exception: `pm up` may be run outside tmux to create or attach to the session

Important rule:

- only one active writer should control a worker pane at a time

The lease model is implemented for manual control and input arbitration. PollyPM records lease ownership and blocks conflicting input, stop, and recovery actions unless explicitly bypassed.

### 5. State store

SQLite tracks:

- known session definitions
- launch records
- operational events
- future heartbeat snapshots and alerts

This gives PollyPM a durable source of truth separate from the terminal UI.

## Logging model

PollyPM should log three streams per worker:

1. pane output stream via `tmux pipe-pane`
2. supervisor-issued actions and injected prompts
3. periodic pane snapshots via `tmux capture-pane`

This scaffold implements the first and the persistent event log foundation.

## Recovery model

PollyPM does not assume it can resume exact internal provider state. Recovery is defined as rebuilding a session from:

- git/worktree state
- session definition
- persistent transcript logs
- checkpoint summaries
- recent operational events

## Configuration model

PollyPM config separates:

- `accounts`: credentials and isolation boundaries
- `promptmaster`: control-plane account selection and failover policy
- `sessions`: role and provider declarations

Worker sessions should be modeled per project, and PollyPM should enforce one active worker session per project at a time.
- `project`: shared tmux and storage settings

This lets PollyPM roles run on any provider:

- session `heartbeat` can use `claude` today and `codex` tomorrow
- session `operator` can switch independently

## MVP build order

### Phase 1

- config file and example project layout
- provider-neutral launch model
- local runtime adapter with per-account home isolation
- tmux bootstrap
- persistent launch/event storage

### Phase 2

- structured heartbeat loop
- pane snapshots
- alert rules for stuck, exited, and repeated-error sessions
- manual control commands

### Phase 3

- Docker runtime adapter
- worktree management
- pane lease ownership
- operator UI

### Phase 4

- project-manager reasoning loop
- scheduling and account balancing
- richer provider telemetry

## Biggest risks

- upstream CLI changes break terminal heuristics
- multiple subscription-account isolation is brittle without containers
- terminal output alone is ambiguous
- over-aggressive intervention reduces trust

## Practical MVP boundary

The first version should answer one question well:

Can PollyPM reliably launch, log, inspect, and restart multiple native CLI agent sessions in tmux while preserving direct human control?

If the answer is yes, the higher-order project-management features are worth adding.
