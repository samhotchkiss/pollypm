---
## Summary

PollyPM uses a single TOML configuration file to declare accounts, sessions, projects, and control-plane settings. Each account is fully isolated with its own home directory, credentials, and environment. The capacity and failover model tracks account health in real time and selects the best available account when a session needs to launch or recover.

---

# 02. Configuration, Accounts, and Isolation

## Configuration File

Configuration is split between two scopes following the "opinionated but pluggable" philosophy:

- **Global** (`~/.pollypm/pollypm.toml`): Control-plane settings, account credentials, project registry, and control sessions. This file declares *what exists* at the system level.
- **Project-local** (`<project>/.pollypm/config/project.toml`): Worker sessions, plugin selections, and project-specific settings. Each project carries its own session plan and behavior configuration.

The key concept: the global config says "these projects exist and here's where they are." PollyPM crawls each project's `.pollypm/config/project.toml` to build the full session plan.

### Global Config: `~/.pollypm/pollypm.toml`

#### `[pollypm]` — Control Plane

```toml
[pollypm]
control_account = "claude-main"
failover_policy = "cross-provider"     # "same-provider" | "cross-provider"
heartbeat_interval_s = 15
snapshot_interval_s = 60
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `control_account` | string | yes | Default account for heartbeat and operator sessions |
| `failover_policy` | enum | no | Whether failover can cross provider boundaries. Default: `cross-provider` |
| `heartbeat_interval_s` | int | no | Seconds between heartbeat checks. Default: `15` |
| `snapshot_interval_s` | int | no | Seconds between pane snapshots. Default: `60` |

#### `[[accounts]]` — Provider Credentials

```toml
[[accounts]]
name = "claude-main"
provider = "claude"
home = "~/.pollypm/homes/claude-main"
role = "controller"                     # "controller" | "worker"
env = { ANTHROPIC_API_KEY = "sk-ant-..." }

[[accounts]]
name = "claude-worker-1"
provider = "claude"
home = "~/.pollypm/homes/claude-worker-1"
role = "worker"
env = { ANTHROPIC_API_KEY = "sk-ant-..." }

[[accounts]]
name = "codex-worker-1"
provider = "codex"
home = "~/.pollypm/homes/codex-worker-1"
role = "worker"
env = { OPENAI_API_KEY = "sk-..." }
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Unique account identifier |
| `provider` | enum | yes | Provider type: `claude`, `codex`, `aider`, `gemini`, `opencode` |
| `home` | path | yes | Isolated home directory for this account |
| `role` | enum | no | Account role. `controller` accounts run control sessions. Default: `worker` |
| `env` | table | no | Environment variables set when launching sessions with this account |

#### `[[projects]]` — Project Registry

The global config registers projects by key and path only. All other project settings live in the project-local config.

```toml
[[projects]]
name = "acme"
path = "/Users/sam/dev/acme"

[[projects]]
name = "widgets"
path = "/Users/sam/dev/widgets"
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Unique project identifier, referenced by sessions |
| `path` | path | yes | Absolute path to the project root |

#### `[[sessions]]` — Control Sessions Only

The global config declares only system-level control sessions (heartbeat, operator). Worker sessions are declared per-project.

```toml
[[sessions]]
name = "heartbeat"
role = "heartbeat"
provider = "claude"
account = "claude-main"
args = ["--model", "sonnet"]
system_prompt = "prompts/heartbeat.md"

[[sessions]]
name = "polly"
role = "operator"
provider = "claude"
account = "claude-main"
args = ["--model", "sonnet"]
system_prompt = "prompts/operator.md"
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Unique session name, used as tmux window name |
| `role` | enum | yes | `heartbeat` or `operator` (control sessions only) |
| `provider` | enum | yes | Which provider CLI to run |
| `account` | string | yes | Account to use for credentials and isolation |
| `args` | list | no | Additional CLI arguments passed to the provider |
| `system_prompt` | path | no | Path to the system/role prompt file |

### Project-Local Config: `<project>/.pollypm/config/project.toml`

Each managed project has its own config file declaring worker sessions, plugin selections, and project-specific settings.

#### `[project]` — Project Settings

```toml
[project]
display_name = "Acme Corp App"
persona = "developer"
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `display_name` | string | no | Human-readable name for TUI display |
| `persona` | string | no | Default persona type for workers in this project |

#### `[[sessions]]` — Worker Sessions

```toml
[[sessions]]
name = "worker-acme"
role = "worker"
provider = "claude"
account = "claude-worker-1"
args = ["--model", "opus"]
system_prompt = "prompts/worker.md"
persona = "developer"

[[sessions]]
name = "worker-acme-2"
role = "worker"
provider = "codex"
account = "codex-worker-1"
args = []
system_prompt = "prompts/worker.md"
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Unique session name, used as tmux window name |
| `role` | enum | yes | `worker` (project-local sessions are always workers) |
| `provider` | enum | yes | Which provider CLI to run |
| `account` | string | yes | Account to use for credentials and isolation |
| `args` | list | no | Additional CLI arguments passed to the provider |
| `system_prompt` | path | no | Path to the system/role prompt file |
| `persona` | string | no | Persona override for this session |

#### `[plugins]` — Backend Selections

```toml
[plugins]
issue_backend = "github-issues"
memory_backend = "sqlite"
doc_backend = "filesystem"
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `issue_backend` | string | no | Issue tracker plugin. Default: `folder-tracker` |
| `memory_backend` | string | no | Memory storage plugin. Default: `sqlite` |
| `doc_backend` | string | no | Documentation plugin. Default: `filesystem` |


## Account Isolation Model

Every account operates in a fully isolated environment. PollyPM enforces this at launch time by constructing the session's environment variables before spawning the provider CLI.

### Home Directory Structure

```
~/.pollypm/
  homes/
    claude-main/
      .claude/              # Claude CLI config
      .config/              # XDG config
      .cache/               # XDG cache
    claude-worker-1/
      .claude/
      .config/
      .cache/
    codex-worker-1/
      .codex/               # Codex config
      .config/
      .cache/
  control-homes/
    heartbeat/
      .claude/
      .config/
    operator/
      .claude/
      .config/
```

### Provider-Specific Environment Variables

Each provider has native configuration directories that PollyPM redirects to the account's home.

| Provider | Environment Variable | Points To |
|----------|---------------------|-----------|
| Claude | `CLAUDE_CONFIG_DIR` | `<account-home>/.claude` |
| Claude | `HOME` | `<account-home>` |
| Codex | `CODEX_HOME` | `<account-home>/.codex` |
| Codex | `HOME` | `<account-home>` |
| Aider | `HOME` | `<account-home>` |
| Gemini | `GEMINI_CONFIG_DIR` | `<account-home>/.gemini` |

### Isolation Guarantees

The following are isolated per account:

- **Authentication**: API keys, OAuth tokens, session cookies
- **Configuration**: Model preferences, CLI settings, plugin state
- **Cache**: Response caches, token caches, temp files
- **Session history**: Conversation logs, command history
- **Provider state**: Any state the provider CLI writes to its home directory

The following are shared across accounts:

- **Project source code**: Workers access the same project directories (or worktrees)
- **PollyPM state store**: SQLite database per project at `<project>/.pollypm/state/pollypm.db`
- **PollyPM logs**: Project-scoped under `<project>/.pollypm/logs/`
- **Git configuration**: System-level git config is inherited (user-level is isolated via HOME)

### Control Session Homes

Heartbeat and operator sessions use dedicated homes under `~/.pollypm/control-homes/`. These are separate from worker account homes to prevent control-session activity from polluting worker account state.

### Project-Local Directory Structure

Each managed project has a single `.pollypm/` directory at its root containing all project-scoped state:

```
<project>/
  docs/                  # Project documentation (committed to git by default)
    project-overview.md
    decisions.md
    architecture.md
    conventions.md
    history.md
    risks.md
    ideas.md
  .pollypm/              # Operational state (gitignored)
    config/              # Project-specific config (plugin selections, overrides, project.toml)
    logs/                # Pane output logs, supervisor logs, snapshots
    state/               # SQLite database, lock files, session-scoped state
    transcripts/         # PollyPM-owned transcript archive (standardized JSONL)
    artifacts/           # Build artifacts, reports, generated outputs
    plugins/             # Project-local plugins (highest discovery precedence)
    rules/               # Project-specific behavioral rules and overrides
    magic/               # Auto-generated configuration and inferred settings
    worktrees/           # Git worktrees for additional agent sessions
    INSTRUCT.md          # Implementation instructions (may contain sensitive details)
```

All state, paths, and locks are session-scoped within `.pollypm/` to support multiple concurrent sessions (a near-term addition).

### Agent Launch Scoping

Agent launches are project-scoped:

- **Main agent**: launches from `<project>/`
- **Additional agents**: launch from `<project>/.pollypm/worktrees/<id>/`
- **Account homes** (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, etc.): used for auth/credentials only, never as launch directories


## Account Capacity Model

PollyPM tracks the capacity state of every account to make informed scheduling and failover decisions.

### Tracked State

| Field | Type | Description |
|-------|------|-------------|
| `current_state` | enum | `healthy`, `exhausted`, `throttled`, `auth_broken`, `provider_outage`, `blocked_no_capacity` |
| `last_usage` | timestamp | When this account last ran a session |
| `reset_time` | timestamp | When exhausted/throttled state is expected to clear |
| `unblock_time` | timestamp | Earliest time this account should be retried |
| `refresh_schedule` | cron | When to re-check capacity proactively |
| `auth_state` | enum | `valid`, `expired`, `revoked`, `unknown` |
| `token_expiry` | timestamp | When the current auth token expires |
| `consecutive_failures` | int | Number of sequential launch/health failures |

### Capacity Probes

Each provider adapter implements a capacity probe that checks whether the account can accept work.

| Provider | Probe Method | Signals |
|----------|-------------|---------|
| Claude | `/usage` endpoint or CLI `--usage` flag | Messages remaining, reset time, plan tier |
| Codex | `/status` endpoint or CLI status command | Requests remaining, rate limit headers |
| Aider | API key validation | Auth validity only (no usage endpoint) |
| Gemini | Quota check endpoint | Requests remaining, daily reset |

Probes run:

- On startup for all accounts
- Before selecting an account for launch or failover
- On the configured `refresh_schedule`
- After any launch failure

### Failure Classification

When an account fails, PollyPM classifies the failure to determine the correct response.

| Classification | Meaning | Action |
|---------------|---------|--------|
| `exhausted` | Usage limits hit | Set `unblock_time` to reset window. Do not retry until then. |
| `throttled` | Rate-limited temporarily | Exponential backoff. Retry after cooldown. |
| `auth_broken` | Credentials invalid or expired | Mark account unusable. Alert operator. |
| `provider_outage` | Provider API is down | Affect all accounts on this provider. Fail over to different provider. |
| `blocked_no_capacity` | No accounts available on any provider | Alert operator. Queue work. Wait for capacity. |


## Failover Selection

When a session needs a new account (recovery, failover, or initial launch), PollyPM evaluates candidates in this order:

### Selection Priority

1. **Healthy non-controller on same provider.** Preserves provider continuity. No prompt reformatting needed.
2. **Healthy non-controller on different provider.** Cross-provider failover. Recovery prompt must be reformatted for the new provider.
3. **Healthy controller on same provider.** Uses the control-plane account. Degrades control-session capacity.
4. **Healthy controller on different provider.** Last resort. Both cross-provider and control-plane degradation.

Within each tier, prefer accounts with:

- Longest time since last usage (spread load)
- Most remaining capacity
- Fewest consecutive failures

### Selection Constraints

- An account already running a session is ineligible for a new session
- An account with `unblock_time` in the future is ineligible
- An account with `auth_state != valid` is ineligible
- If no account is eligible, the session enters `blocked_no_capacity` and the operator is alerted

### Recovery Priority

When multiple sessions need recovery simultaneously, PollyPM prioritizes:

1. **Heartbeat supervisor** — must be running for anything else to be monitored
2. **Operator** — needed to coordinate further recovery
3. **Human-interrupted workers** — a human was actively working; restore their session first
4. **Preempted workers** — workers that were stopped to free an account for higher-priority recovery
5. **New work** — previously unstarted sessions


## Capacity Registry

Account capacity state is persisted in the SQLite state store in the `account_capacity` table.

| Column | Type | Description |
|--------|------|-------------|
| `account_name` | text PK | References the account in config |
| `state` | text | Current capacity classification |
| `last_probe_at` | timestamp | When capacity was last checked |
| `reset_at` | timestamp | When current limitation is expected to clear |
| `remaining_messages` | int | Messages remaining (if provider reports) |
| `remaining_tokens` | int | Tokens remaining (if provider reports) |
| `consecutive_failures` | int | Sequential failure count |
| `metadata` | json | Provider-specific capacity data |

The capacity registry is the source of truth for failover decisions. Config defines accounts; the registry tracks their live state.


## Resolved Decisions

1. **TOML, not YAML.** TOML is Python-native (stdlib `tomllib`), less ambiguous than YAML, and maps cleanly to the hierarchical config structure. YAML's implicit typing and indentation sensitivity create unnecessary failure modes.

2. **Per-account home isolation.** Each account gets a dedicated home directory with provider-specific env vars redirected. This prevents credential leakage, config interference, and session history cross-contamination between accounts. The alternative (shared home with namespace prefixes) was rejected as fragile and provider-dependent.

3. **Cross-provider failover allowed.** When a provider's accounts are all exhausted, PollyPM can fail over to a different provider. This maximizes availability at the cost of recovery prompt reformatting. The `failover_policy` setting allows restricting to same-provider if desired.

4. **Controller accounts eligible last.** Controller accounts (used for heartbeat and operator) are eligible for worker failover, but only as a last resort. This prevents a cascade where worker failover starves the control plane.

5. **File-based capacity registry.** Capacity state is stored in SQLite alongside other operational state. No external service or file-watching needed. The registry is updated on every probe and consulted on every account selection.


## Cross-Doc References

- Architecture and system roles: [01-architecture-and-domain.md](01-architecture-and-domain.md)
- Session lifecycle and tmux integration: [03-session-management-and-tmux.md](03-session-management-and-tmux.md)
