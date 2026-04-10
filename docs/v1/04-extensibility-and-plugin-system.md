---
## Summary

PollyPM is a pluggable orchestration platform. The core is headless; frontends, backends, and integrations are all swappable. A five-layer architecture separates durable domain logic from extension discovery, service exposure, presentation, and plugin-provided behavior. Plugins use the same contracts as built-in components, and a versioned plugin API guarantees stability across upgrades. Repo-local plugins take highest precedence so individual projects can carry custom behavior without modifying the package tree.

---

# 04. Extensibility and Plugin System

## Five-Layer Architecture

PollyPM is organized into five distinct layers. Each layer has a clear responsibility boundary, and dependencies flow strictly downward.

| Layer | Name | Responsibility |
|-------|------|----------------|
| 1 | Core | Domain logic and durable state |
| 2 | Extension Host | Plugin lifecycle and hook routing |
| 3 | Service API | Stable interface for all callers |
| 4 | Frontends | Presentation and interaction |
| 5 | Plugins | Behavior extensions via defined interfaces |


## Layer 1: Core

The core is the runtime/API substrate. It owns all durable domain behavior and runtime primitives:

- **Tmux/session/process primitives**: session windows, pane lifecycle, process management
- **Transcript and pane access**: ingestion, normalization, and access to pane output and transcript archives
- **LLM account/session plumbing**: auth, credentials, session routing, provider connections
- **Scheduling/cron trigger**: recurring, delayed, and one-shot orchestration jobs
- **Durable state store**: SQLite-backed event log, heartbeats, launches, alerts, checkpoints
- **Stable internal API**: defined surface that plugins call — plugins never reach into core internals
- **Projects**: declarations, bindings, directory mappings
- **Sessions**: lifecycle state machine, role assignment, provider binding
- **Accounts**: credentials, capacity tracking, cooldown timers, isolation paths
- **Leases**: human/automation ownership arbitration per session
- **Alerts**: structured alert creation, acknowledgment, resolution
- **Checkpoints**: recovery snapshots of git state, transcript excerpts, operational context
- **Event emission**: all state transitions, health changes, and operator actions

Plugins are policy engines — they call core APIs to implement behavior, but never reach into core internals. The core has no knowledge of the caller. It does not know whether it is being driven by the Textual TUI, a WebSocket client, a Discord bot, or a test harness. This separation is the foundation that makes everything else pluggable.


## Layer 2: Extension Host

The extension host manages the plugin lifecycle and provides failure isolation between plugins and the core.

Responsibilities:

- **Plugin discovery**: scan all plugin locations in precedence order
- **Manifest validation**: verify API version compatibility, required fields, entrypoint resolution
- **Capability registration**: record which plugins provide which interfaces
- **Hook routing**: deliver lifecycle events to matching observers and filters
- **Failure isolation**: catch and log plugin exceptions without crashing the core; disable misbehaving plugins after repeated failures

The extension host is the only component that imports plugin code. The core never directly references a plugin module.


## Layer 3: Service API

The service API is a stable in-process and local-transport interface that every frontend and automation script uses. It is the single entry point for all commands and queries.

The service API is the single stable layer that ALL transports wrap:

- **CLI commands** (v1 -- primary interface for both humans and agents)
- **MCP tools** (future -- structured, typed agent access)
- **HTTP/WebSocket** (future -- web and mobile clients)
- **Discord/Slack gateway** (future)

Every transport is a thin adapter that translates its native format into service API calls. Adding a new transport does not require changes to core or plugin code.

Exposed operations:

| Operation | Purpose |
|-----------|---------|
| `list_projects()` | Enumerate declared projects |
| `list_sessions()` | Enumerate sessions with current state |
| `send_input(session_name, text, owner)` | Deliver operator or automation input to a session |
| `claim_lease(session_name, owner)` | Take input ownership of a session |
| `refresh_usage(account_name)` | Trigger capacity and usage refresh for an account |
| `create_worker(project_key, prompt, account_hint, provider_hint)` | Launch a new worker session |
| `route_inbox_reply(thread_id, reply_text)` | Deliver a reply to an inbox conversation thread |
| `subscribe_events(cursor, filters)` | Stream events from a cursor position with optional filters |

This layer is the seam between the core and all consumers: TUI, future Web UI, Discord integration, CLI commands, and automation scripts.


### Transport Architecture

All transports converge on the service API as the single stable boundary:

```
CLI commands  ─┐
MCP tools     ─┼──→ Service API ──→ Core
HTTP/WS       ─┤
Chat gateways ─┘
```

v1 ships CLI as the only transport. MCP and HTTP are planned for future versions. Because every transport calls the same service API, behavior is identical regardless of how the caller connects.


## Layer 4: Frontends

Frontends are thin clients over the service API. They own presentation and interaction patterns but contain no business logic.

| Frontend | Transport | Status |
|----------|-----------|--------|
| Textual TUI | In-process | Current |
| Web UI | HTTP + WebSocket | Future |
| Discord bot | Discord gateway | Future |
| Mobile/remote | TBD | Future |

Frontends own:

- Layout, rendering, and visual presentation
- Input handling and local interaction patterns
- Transport and session authentication for the frontend itself

Frontends do not own:

- Business rules or domain logic
- Provider launch construction
- Task state transitions
- Memory writes or queries


## Layer 5: Plugins

Plugins extend defined interfaces without modifying core code. PollyPM defines eleven plugin families, each with a specific interface contract.


## Plugin Families

### Provider

Provider plugins add support for new CLI agents such as Gemini, Aider, or OpenCode.

Interface:

- **Binary detection**: locate the provider CLI on the system
- **Launch command construction**: build the shell command and environment to start a session
- **Resume command construction**: build a command to reattach or restart a session in place
- **Transcript discovery**: declare where JSONL session logs are written
- **Usage extraction**: parse capacity and token usage from pane output or API responses
- **Health classification**: interpret provider-specific signals into normalized health states

Built-in providers (Claude, Codex) implement this same interface.

### Runtime

Runtime plugins control where and how a session executes.

Interface:

- **Environment preparation**: set up the execution environment (directories, mounts, isolation)
- **Command wrapping**: wrap the provider launch command for the target runtime
- **Path mapping**: translate between host paths and runtime-internal paths
- **Auth mount**: make credentials and config available inside the runtime

Examples: local host runtime (default), Docker runtime, SSH remote runtime.

### Scheduler / Cron

Scheduler plugins manage recurring, delayed, and one-shot orchestration jobs without hardwiring a timing loop into the core.

Interface:

- **Register recurring job**: schedule a job to run at a fixed interval or cron expression
- **Register delayed job**: schedule a one-shot job after a delay
- **Register one-shot job**: schedule a job for a specific time
- **Cancel job**: remove a scheduled job
- **Pause/resume job**: temporarily suspend or reactivate a job
- **List jobs**: enumerate all scheduled jobs with their state and next-run time

Typical jobs: usage refreshes, delayed retries, inbox digests, project check-ins, automatic resumes when capacity returns.

### Heartbeat

Heartbeat plugins execute session monitoring without coupling it to one local loop.

Interface:

- **Enumerate sessions**: list sessions that require monitoring
- **Schedule cadence**: configure check intervals per session or role
- **Collect signals**: gather health signals from transcript tails, tmux pane liveness, process checks, and account capacity
- **Normalize results**: produce structured heartbeat results with health classification

The core owns heartbeat policy (what to do about unhealthy sessions). Heartbeat plugins own heartbeat execution (how to detect health).

### Transcript Source

Transcript source plugins make JSONL session logs the primary monitoring source.

Interface:

- **Discover streams**: find transcript files for a given session
- **Tail events**: stream new events since a cursor position
- **Parse normalized events**: convert provider-specific log entries into normalized event types

Normalized transcript event types:

| Event | Description |
|-------|-------------|
| `user_turn` | Human or automation input delivered to the agent |
| `assistant_turn` | Agent response text |
| `tool_call` | Agent invoked a tool |
| `tool_result` | Tool returned a result |
| `token_usage` | Token consumption report |
| `session_state` | Session state transition (idle, working, stuck, exited) |
| `turn_end` | Boundary marker between conversation turns |
| `error` | Agent or provider error |

### Memory

Memory plugins allow the smart memory system to use different storage backends.

Interface:

- `remember(scope, item)` — store a memory item
- `recall(scope, query, limit)` — retrieve relevant memories
- `summarize(scope)` — produce a summary of stored memories
- `compact(scope)` — consolidate and compress stored memories
- `delete(scope, id)` — remove a specific memory item

Memory scopes:

- **Global**: applies across all projects and sessions
- **Project**: scoped to a single project
- **Issue**: scoped to a single task or issue
- **Session**: scoped to a single agent session
- **Inbox thread**: scoped to a single conversation thread

Default backend: local file + SQLite. Possible alternates: vector store, Postgres, remote hosted service.

### Task

Task plugins allow the file-based issue tracker to be replaced with external systems.

Interface:

- **List tasks**: query tasks by state, project, or assignee
- **Create task**: add a new task with metadata
- **Move task**: transition a task between states
- **Append notes**: add progress notes or context to a task
- **Attach handoff metadata**: record handoff context for session transitions
- **Query next**: find the next available task for a given project or priority

Default backend: local folder tracker under `issues/`. Possible alternates: GitHub Issues, Linear, Jira.

### Agent Profile

Agent profile plugins make agent behavior and identity pluggable instead of baking it into fixed prompts.

Interface:

- `system_prompt_blocks()` — return ordered blocks for the system prompt
- `behavior_rules()` — return behavioral constraints and preferences
- `preferred_provider()` / `preferred_model()` / `preferred_reasoning_level()` — declare execution preferences
- `capability_policy()` / `memory_policy()` / `task_policy()` / `review_policy()` — declare operational policies

Profiles are composable: built-in role base, user-local override, project-local override, session-local override. Provider/model is what brain is running. Agent profile is how that brain behaves.

### Frontend Transport

Frontend transport plugins let new UIs speak to the PM core.

Interface:

- **Receive actions**: accept operator commands from the frontend
- **Present responses**: deliver PM responses in frontend-appropriate format
- **Subscribe events**: stream the event bus to the frontend
- **Map identity**: translate frontend-specific user/channel identity to PollyPM operator identity

Examples: local WebSocket server for browser UI, Discord gateway, Slack gateway.

### Skill / MCP

Skill and MCP plugins expose external capabilities to Polly and workers through policy-controlled catalogs.

Interface:

- Discover available capabilities
- Describe invocation requirements and input schemas
- Attach allowed scopes (per-project, per-role, global)
- Render capability hints into agent prompts
- Optionally broker actual invocation

Supports: built-in skills, repo-local skills, user-local skills, MCP servers with per-project policy.


## Hook and Filter Architecture

PollyPM exposes lifecycle hooks with two modes: observers and filters.

### Observers

Observers see events and emit side effects but cannot change the primary action.

Use cases:

- Analytics logging
- Discord or Slack notification fanout
- Usage metering
- Audit trail recording

### Filters

Filters can enrich, mutate, veto, or defer actions.

Use cases:

- Block launching a worker on a forbidden project
- Inject project-specific instructions before session input
- Reroute a task to a different backend based on labels
- Redact sensitive text before memory write

### Key Hook Points

| Category | Hook Points |
|----------|-------------|
| App lifecycle | `app.starting`, `app.started`, `shutdown` |
| Account events | `account.connected`, `account.usage_refreshed` |
| Session events | `session.before_launch`, `session.after_launch`, `session.before_input`, `session.after_input`, `session.transcript_event`, `session.turn_ended`, `session.recovery_requested`, `session.failover_requested` |
| Memory/task transitions | `memory.before_write`, `memory.after_write`, `task.before_transition`, `task.after_transition` |
| Inbox | `inbox.message_opened`, `inbox.reply_received` |
| Alerts | `alert.raised` |
| Frontend | `frontend.command_received` |

### Filter Return Model

Filters return an explicit disposition:

| Return | Effect |
|--------|--------|
| `allow` | Proceed with the action unchanged |
| `mutate` | Proceed with a modified payload |
| `deny` | Cancel the action; the caller receives a denial reason |
| `defer` | Pause the action for later re-evaluation |


## Plugin Discovery

PollyPM searches four locations in strict precedence order:

| Priority | Location | Purpose |
|----------|----------|---------|
| 1 (highest) | `<project>/.pollypm/plugins/` | Project-local: project carries custom behavior |
| 2 | `~/.pollypm/plugins/` | User-local: personal plugins that survive upgrades |
| 3 | Python package entrypoints | Distributed plugins installed via pip/uv |
| 4 (lowest) | Built-in plugins | Default fallback shipped with PollyPM |

When multiple plugins provide the same capability, the highest-precedence plugin wins. This allows a project to override a built-in provider or a user to override default behavior without modifying the package tree.


## Automated Plugin Validation

Every plugin must pass an automated validation harness before activation. The harness exercises all interface methods declared in the plugin's `capabilities` list:

- For each declared capability, the harness instantiates the plugin and calls every method in the corresponding interface contract with synthetic inputs.
- Methods must return values conforming to the expected types (e.g., `LaunchCommand`, `ProviderUsageSnapshot`, `TranscriptSource`).
- Methods must not raise unhandled exceptions during validation.
- Plugins that fail validation are disabled with a logged reason. They are not silently ignored — the operator is alerted.
- Validation runs at discovery time (startup and `pm plugin reload`) and can be triggered manually via `pm plugin validate <name>`.

This ensures that no plugin reaches production without proving it can fulfill its declared contract.


## Override Hierarchy

All configurable behavior follows a strict override hierarchy:

| Priority | Source | Location | Mutability |
|----------|--------|----------|------------|
| 1 (lowest) | Built-in defaults | Shipped with PollyPM | Never modified — read-only baseline |
| 2 | User-global overrides | `~/.pollypm/` | User preferences, personal tweaks |
| 3 (highest) | Project-local overrides | `<project>/.pollypm/config/` | Project-specific behavior |

Key rules:

- **Patches create overrides, never modify built-ins.** When behavior is changed, a new override entry is written at the appropriate scope. Built-in defaults are immutable.
- **Higher-precedence sources win.** A project-local override beats a user-global override, which beats a built-in default.
- **The agent is the configuration interface.** When a user disagrees with behavior, the agent asks if they want to change it and patches the relevant override file at the correct scope. Users should not need to hand-edit override files.
- This hierarchy applies to: plugin selections, behavioral rules, agent profiles, scheduling policies, memory/task backend choices, and all other configurable behavior.


## Plugin Manifest

Each plugin includes a `pollypm-plugin.toml` manifest:

```toml
api_version = "1"
name = "gemini-cli"
kind = "provider"
version = "0.1.0"
entrypoint = "gemini_plugin:plugin"
capabilities = ["provider", "transcript_source"]
description = "Provider adapter for the Gemini CLI agent"
author = "PollyPM Contributors"
```

Manifest fields:

| Field | Required | Description |
|-------|----------|-------------|
| `api_version` | Yes | Plugin API version this plugin targets |
| `name` | Yes | Unique plugin identifier |
| `kind` | Yes | Primary plugin family |
| `version` | Yes | Plugin version (semver) |
| `entrypoint` | Yes | Python dotted path to the plugin object |
| `capabilities` | Yes | List of interfaces this plugin provides |
| `description` | No | Human-readable description |
| `author` | No | Plugin author or maintainer |


## Stability Contract

PollyPM exports two versioned public API namespaces:

- `pollypm.plugin_api.v1` — interfaces and base classes for plugin authors
- `pollypm.service_api.v1` — operations and types for service API consumers

Everything outside these namespaces is internal and not stable for external use. Internal modules may change structure, rename functions, or reorganize without notice between releases. Plugins that import from internal modules do so at their own risk.


## Resolved Decisions

1. **Five-layer architecture.** The split into core, extension host, service API, frontends, and plugins provides clean dependency direction and testability. Each layer can be developed, tested, and replaced independently.

2. **Headless core.** The core has no knowledge of any frontend. This was chosen over a TUI-centric design to enable Web UI and Discord as first-class frontends rather than afterthoughts.

3. **Plugins use the same contracts as built-ins.** Built-in providers, memory backends, and task backends implement the same interfaces that third-party plugins use. There is no privileged internal API for built-ins. This ensures the plugin interfaces are complete and battle-tested.

4. **Project-local plugins take highest precedence.** A project can carry its own provider adapter, hook, or profile override in `<project>/.pollypm/plugins/` without modifying the user's global configuration or the PollyPM package. This supports per-project customization in multi-project workflows.

5. **Versioned plugin API.** The `pollypm.plugin_api.v1` and `pollypm.service_api.v1` namespaces are the stability boundary. Breaking changes require a new version namespace. This protects plugin authors from internal refactoring.


## Cross-Doc References

- Core architecture and domain model: [01-architecture-and-domain.md](01-architecture-and-domain.md)
- Provider SDK and adapter contract: [05-provider-sdk.md](05-provider-sdk.md)
