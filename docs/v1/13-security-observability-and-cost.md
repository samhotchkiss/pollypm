---
## Summary

PollyPM manages multiple agent sessions across multiple accounts on a single host. This document defines the security boundaries that isolate those sessions and accounts, the observability stack that makes system behavior visible, and the cost tracking mechanisms that account for token usage. The core security principle is that PollyPM never handles raw credentials — provider CLIs manage their own auth within isolated home directories.

---

# 13. Security, Observability, and Cost

## Security Model

### Threat Model

PollyPM operates in a trusted single-host environment. The primary security concerns are:

- **Cross-account contamination**: One account's credentials or state leaking to another account's session
- **Cross-session interference**: One agent session reading or writing to another session's files or state
- **Credential exposure**: API keys or tokens appearing in logs, config files, or checkpoint data
- **Plugin misbehavior**: A buggy or malicious plugin accessing state it should not
- **Stale credentials**: Expired or revoked credentials causing silent failures

PollyPM does not defend against a compromised host operating system. If the OS is compromised, all bets are off. The isolation model is defense-in-depth for operational correctness, not a security sandbox against adversaries with root access.

### Account Isolation

Each account in PollyPM gets an isolated home directory. This directory holds authentication and credentials only — it is not the working directory for agent sessions.

```
~/.pollypm/homes/
  claude-main/         # Account home for "claude-main"
    .config/claude/    # Claude CLI config and auth
  claude-alt/          # Account home for "claude-alt"
    .config/claude/    # Separate Claude CLI config and auth
  codex-work/          # Account home for "codex-work"
    .codex/            # Codex CLI config and auth
```

Isolation properties:

- Each session sets `HOME` to the account's home directory so provider CLIs find their auth credentials
- Agent sessions launch and execute from the **project directory** (or worktree), not the account home
- Account homes contain auth/credentials only — no project files, no working state
- Provider CLIs store their auth tokens, config files, and caches within their `HOME`
- No session can see or access another account's home directory
- Account home directories are created with mode `700` (owner read/write/execute only)
- PollyPM core reads account home directories only to verify they exist and have correct permissions

### Session Isolation

Each tmux window is an isolated execution context:

- Separate shell process with its own environment variables
- `HOME` set to the account's home directory
- Working directory set to the project directory or worktree
- Provider-specific environment variables scoped to the session

The input lease model (doc 03) provides an additional isolation layer:

- Only one entity (automation or human) holds the input lease at a time
- Cross-session input injection is not possible through the lease system
- PollyPM's prompt injection targets specific tmux panes by window ID

### Plugin Isolation

Plugins (doc 04) run within the PollyPM Python process but are isolated by convention and error handling:

- Plugin failures are caught at the plugin boundary and do not crash PollyPM core
- Plugins receive only the data they are registered to handle (event types, hook points)
- Plugins cannot access other plugins' internal state
- Plugin errors are logged and surfaced as alerts, not silently swallowed
- A misbehaving plugin can be disabled without restarting PollyPM

### Config Security

The `pollypm.toml` configuration file contains structural declarations but never raw credentials:

```toml
# This is safe — it references an account by name, not by credential
[accounts.claude-main]
provider = "claude"
home = "~/.pollypm/homes/claude-main"

# This is NEVER allowed — raw API keys do not belong in config
# api_key = "sk-ant-..."  # DO NOT DO THIS
```

Rules:

- `pollypm.toml` may contain account names, provider types, home directory paths, and behavioral settings
- `pollypm.toml` must never contain API keys, tokens, passwords, or other raw credentials
- Authentication is handled by provider CLIs within their isolated home directories using provider-native auth flows
- If a provider requires an API key environment variable, it is set from a secure source (keychain, env file within the account home) at launch time, not stored in `pollypm.toml`

### Credential Handling Rules

| Rule | Rationale |
|------|-----------|
| PollyPM never stores API keys or tokens directly | Avoids becoming a credential management system |
| Provider CLIs handle their own auth | Each provider has its own auth flow; PollyPM does not need to understand it |
| Account home directories have mode 700 | Prevents other system users from reading credentials |
| `.pollypm/` is gitignored by default | Prevents accidental commit of logs, checkpoints, and state data |
| Pane logs may contain sensitive output | Log retention policies should account for this |
| Checkpoint summaries must not include raw credentials | Summary generation prompts explicitly exclude credential-like strings |


## Observability Stack

PollyPM provides visibility into system behavior through five complementary channels.

### Session Logs

Per-session tmux pane logs capture everything an agent session outputs.

- Location: `.pollypm/logs/<session-id>/<launch-id>/pane.log`
- Format: Raw terminal output, including ANSI escape codes
- Created by: `tmux pipe-pane` configured at session launch
- Retention: Configurable, default indefinite
- Use: Debugging agent behavior, reconstructing session history, feeding checkpoint generation

### Event Log

All operational events are recorded in the SQLite state store.

Event types:

| Category | Events |
|----------|--------|
| Session lifecycle | `session_launched`, `session_stopped`, `session_exited`, `session_recovered` |
| Health | `health_changed`, `heartbeat_recorded`, `health_alert_raised`, `health_alert_resolved` |
| Failover | `failover_initiated`, `failover_completed`, `failover_failed` |
| Account | `account_capacity_changed`, `account_cooldown_started`, `account_cooldown_ended`, `account_auth_failed` |
| Checkpoint | `checkpoint_created`, `checkpoint_promoted`, `recovery_prompt_built` |
| Lease | `lease_acquired`, `lease_released`, `lease_transferred` |
| Operator | `operator_command`, `operator_alert_ack`, `queue_item_assigned` |

Each event record contains:

- Event ID (auto-incrementing)
- Event type
- Timestamp
- Session ID (if applicable)
- Project (if applicable)
- Payload (JSON blob with event-specific data)
- Source (which component emitted the event)

### Heartbeat Snapshots

The heartbeat supervisor (doc 10) captures periodic state for each active session:

- Pane content snapshot via `tmux capture-pane`
- Health classification result
- Session responsiveness metrics
- Stored as timestamped files and recorded in the heartbeats table

### Checkpoint Artifacts

Structured recovery data created at meaningful boundaries (doc 12):

- Machine-readable JSON files in `<project>/.pollypm/artifacts/checkpoints/`
- Human-readable markdown summaries for Level 1 and Level 2 checkpoints
- Indexed in the SQLite checkpoints table

### TUI Dashboard

The Textual-based TUI provides real-time visibility:

- Session list with current state, health, provider, account, and project
- Active alerts with severity, age, and recommended action
- Event stream showing recent operational events
- Session detail view with pane preview, checkpoint history, and launch history
- Account status showing capacity, cooldown, and auth health


## Alert Pipeline

Alerts are the mechanism by which PollyPM surfaces problems that require attention.

### Alert Flow

```
Heartbeat detects anomaly
  → Creates alert event in state store
    → TUI displays alert in dashboard
      → Operator (human or PM agent) acts on alert
        → Resolution recorded in state store
```

### Alert Types

| Alert Type | Trigger | Severity | Automated Response |
|------------|---------|----------|--------------------|
| `session_stuck` | No progress detected for N heartbeat cycles | warning | None (operator decides) |
| `session_exited` | Provider CLI process exited | error | Auto-recovery if policy allows |
| `session_looping` | Repeated identical output detected | warning | None (operator decides) |
| `capacity_exhausted` | Account hit usage or rate limits | warning | Failover to alternate account |
| `auth_broken` | Provider CLI reports auth failure | error | Failover to alternate account |
| `provider_outage` | Multiple accounts for same provider failing | critical | Cross-provider failover if available |
| `drift_detected` | Agent work diverging from task description | warning | None (PM review needed) |
| `progress_stalled` | Meaningful progress stopped across all sessions | critical | None (operator escalation) |
| `rapid_recovery_loop` | Session failed and recovered N times quickly | error | Pause recovery, escalate |

### Alert Properties

Every alert record contains:

| Field | Type | Description |
|-------|------|-------------|
| `alert_id` | int | Unique identifier |
| `alert_type` | string | One of the types above |
| `severity` | string | info, warning, error, critical |
| `session_id` | string | Affected session (if applicable) |
| `project` | string | Affected project (if applicable) |
| `message` | string | Human-readable description |
| `created_at` | datetime | When the alert was created |
| `resolved_at` | datetime | When the alert was resolved (null if open) |
| `resolution` | string | How the alert was resolved |
| `auto_action_taken` | string | What automated response was triggered (if any) |

### Alert Durability

Alerts are durable. They are stored in the SQLite state store, not just displayed in the TUI.

- Alerts persist across TUI restarts
- Unresolved alerts are displayed immediately when the TUI starts
- Alert history is queryable via CLI commands
- Alert counts and states are available to the operator agent for decision-making

### Alert Policies

Alert responses can be configured per-alert-type:

```toml
[alerts.session_exited]
auto_recover = true
max_rapid_recoveries = 3

[alerts.capacity_exhausted]
auto_failover = true

[alerts.session_stuck]
auto_recover = false   # Requires operator judgment
notify_operator = true
```

Policies determine whether an alert triggers automated action or waits for operator intervention.


## Cost Tracking

PollyPM tracks token usage to provide visibility into operational costs and enable future budget management.

### Transcript Archive

PollyPM owns its own transcript archive at `<project>/.pollypm/transcripts/`. Provider CLIs write their native transcript files; PollyPM copies or indexes relevant transcript data into this archive for unified access. This ensures transcript data survives provider CLI upgrades, format changes, and account rotation.

### Token Extraction

Token usage data is extracted from provider JSONL transcript files:

- Claude CLI writes JSONL transcripts that include input/output token counts per turn
- Codex CLI provides similar transcript data
- Each provider adapter knows how to parse its provider's transcript format
- Token extraction runs asynchronously — it does not block session operation

### Transcript Ledger

The transcript ledger is a JSONL-based token accounting system:

```jsonl
{"ts":"2026-04-09T10:00:00Z","session":"worker-1","project":"pollypm","account":"claude-main","input_tokens":2400,"output_tokens":800,"model":"opus-4","cost_usd":0.054}
{"ts":"2026-04-09T10:01:00Z","session":"worker-1","project":"pollypm","account":"claude-main","input_tokens":3100,"output_tokens":1200,"model":"opus-4","cost_usd":0.078}
```

Each entry records:

- Timestamp
- Session ID
- Project
- Account
- Input and output token counts
- Model used
- Estimated cost (based on published pricing, best-effort)

### Usage Aggregation

Token usage is aggregated along multiple dimensions:

| Dimension | Use Case |
|-----------|----------|
| Per-session | How much did this session cost? |
| Per-project | How much have we spent on this project? |
| Per-account | How much of this account's capacity have we used? |
| Per-provider | How much are we spending on Claude vs. Codex? |
| Per-time-period | Daily, weekly, monthly cost trends |

Aggregation is computed on demand from the ledger, not pre-computed. The ledger is append-only and is the source of truth.

### Cost Visibility

Current cost data is available through:

- TUI dashboard: summary panel showing recent usage by project and account
- CLI commands: `pm cost` with various grouping and filtering options
- Checkpoint metadata: each checkpoint records cumulative token usage for the session

### Future: Budget Policies

Budget policies are a planned feature for controlling cost allocation:

- Per-project token budgets with alerts at thresholds
- Account assignment rules: "use cheap accounts for exploration, expensive accounts for merge-critical work"
- Automatic session pause when budget is exceeded
- Budget carryover and reset periods

Budget policies are not implemented in v1 but the ledger infrastructure supports them.

### Future: Usage Forecasting

Usage forecasting is a planned feature for capacity planning:

- Estimate remaining account lifetime at current burn rate
- Predict when an account will hit rate limits or usage caps
- Recommend account rotation schedules
- Alert before capacity exhaustion rather than after

Forecasting depends on having sufficient historical data in the ledger.


## Log Hygiene

### Sensitive Data in Logs

Pane logs capture raw terminal output, which may include:

- File contents with sensitive data
- Environment variable values printed by debug commands
- API responses containing private information

Mitigation:

- `.pollypm/` is gitignored by default, preventing accidental commits
- Log files inherit the permissions of the `.pollypm/` directory
- Log retention policies allow automatic pruning of old logs
- Checkpoint summaries are generated with instructions to exclude credential-like patterns

### Log Retention

| Log Type | Default Retention | Configurable |
|----------|------------------|--------------|
| Pane logs | Indefinite | Yes |
| Event log (SQLite) | Indefinite | Yes |
| Heartbeat snapshots | 24 hours | Yes |
| Level 0 checkpoints | 24 hours | Yes |
| Level 1+ checkpoints | Indefinite | Yes |
| Transcript ledger | Indefinite | Yes |

Retention is managed by a maintenance command (`pm maintenance prune`) that can be run manually or scheduled.


## Opinionated but Pluggable

The security, observability, and cost tracking systems described in this document are PollyPM's opinionated defaults. They represent strong out-of-the-box behavior, but every policy layer is replaceable:

- **Alert policies** are configurable per-alert-type (as shown above) and can be replaced entirely via custom alert handler plugins.
- **Log retention** schedules are configurable defaults. Users can override retention periods per log type or replace the pruning strategy.
- **Cost tracking backends** default to the JSONL transcript ledger. Users can plug in alternative cost tracking backends (e.g., forwarding to an external billing system) via the plugin system.
- **Security policies** — including credential handling rules and isolation requirements — are documented defaults. Projects with different threat models can override specific policies in project-local configuration.

This pattern — strong defaults that are fully replaceable — applies throughout PollyPM. Checkpoint strategy, security policies, testing requirements, and migration approach are all configurable and overridable. PollyPM ships opinionated defaults so the system works out of the box, but no default is sacred.


## Resolved Decisions

1. **PollyPM never handles raw credentials.** Provider CLIs manage their own authentication within their isolated home directories. PollyPM references accounts by name, never by key. This avoids building a credential management system and reduces the blast radius of any PollyPM vulnerability.

2. **Account homes are the isolation boundary.** Setting `HOME` per-session is the primary isolation mechanism. It is simple, works with all provider CLIs, and leverages each provider's existing config/auth conventions. Container-level isolation is a future enhancement, not a requirement.

3. **Alerts are durable, not ephemeral.** Alerts are stored in SQLite and persist across TUI restarts. This ensures no alert is lost due to a TUI crash or operator absence. Alert history is queryable and available to automated systems.

4. **Token tracking via JSONL, not API polling.** Token usage is extracted from provider transcript files after the fact, not by intercepting or polling provider APIs. This is non-intrusive, works offline, and does not require provider API access beyond what the CLI already has.

5. **`.pollypm/` is always gitignored.** The project-local `.pollypm/` directory contains logs, transcripts, checkpoints, and operational data that should never be committed. The gitignore entry is created automatically and is not optional.

6. **`docs/` is committed, `.pollypm/INSTRUCT.md` is not.** Project documentation in `<project>/docs/` is committed to git by default — it is valuable shared knowledge (project overview, decisions, architecture, conventions, history, risks, ideas). Because `docs/` is committed, it must NEVER contain secrets, credentials, API keys, or sensitive operational details. Sensitive implementation instructions — deployment procedures, environment setup, test credentials — belong in `<project>/.pollypm/INSTRUCT.md`, which is gitignored along with the rest of `.pollypm/`.


## Cross-Doc References

- Account isolation and home directory model: [02-configuration-accounts-and-isolation.md](02-configuration-accounts-and-isolation.md)
- Session management and tmux integration: [03-session-management-and-tmux.md](03-session-management-and-tmux.md)
- Plugin isolation and error handling: [04-extensibility-and-plugin-system.md](04-extensibility-and-plugin-system.md)
- Provider adapter interface: [05-provider-sdk.md](05-provider-sdk.md)
- Heartbeat monitoring and health classification: [10-heartbeat-and-supervision.md](10-heartbeat-and-supervision.md)
- Checkpoint data and recovery flow: [12-checkpoints-and-recovery.md](12-checkpoints-and-recovery.md)
- Testing and verification requirements: [14-testing-and-verification.md](14-testing-and-verification.md)
