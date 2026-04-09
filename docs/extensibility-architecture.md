# PollyPM Extensibility Architecture

## Goal

Make PollyPM a pluggable orchestration platform instead of a fixed tmux/TUI app. The core should support:

- alternate frontends like a Web UI and Discord bot
- replaceable memory backends
- replaceable task-management backends
- drop-in provider adapters for new CLI agents like Gemini
- hook/filter plugins that can observe, enrich, mutate, or veto behavior
- local user plugins that survive upgrades

## Design Principles

1. The core should be headless.
2. Frontends should talk to the same control API.
3. Every integration point should be a versioned interface.
4. Built-ins and plugins should use the same contracts.
5. User-installed plugins must live outside the package tree so upgrades do not overwrite them.
6. State, transcript ingestion, memory, and task tracking should be separate concerns.

## Core Shape

PollyPM should be split into five layers:

1. `core`
2. `extension_host`
3. `service_api`
4. `frontends`
5. `plugins`

### 1. Core

The core owns durable domain behavior:

- projects
- sessions
- accounts
- leases
- alerts
- checkpoints
- transcript ingestion
- event emission

The core should not know whether the caller is the TUI, a WebSocket client, Discord, or a CLI command.

### 2. Extension Host

The extension host loads plugins, validates manifests, resolves dependencies, and runs hooks safely.

Responsibilities:

- discover plugins from built-in, repo-local, user-local, and package-entrypoint locations
- validate plugin API compatibility
- register capabilities
- route hooks to matching plugins
- isolate plugin failures so one broken plugin does not take down the core

### 3. Service API

Expose a stable in-process and local-transport API that every frontend uses.

Examples:

- `list_projects()`
- `list_sessions()`
- `open_session(project_or_session)`
- `send_input(session_name, text, owner)`
- `claim_lease(session_name, owner)`
- `release_lease(session_name)`
- `refresh_usage(account_name)`
- `create_worker(project_key, prompt, account_hint, provider_hint)`
- `route_inbox_reply(thread_id, reply_text)`
- `subscribe_events(cursor, filters)`

This becomes the seam for:

- TUI
- Web UI
- Discord integration
- automation scripts

### 4. Frontends

Frontends should be thin clients over the service API.

Examples:

- Textual cockpit
- browser UI
- Discord bot
- future mobile/remote operator clients

Frontends should own:

- presentation
- local interaction patterns
- transport/session auth for the frontend itself

Frontends should not own:

- business rules
- provider launch logic
- task state transitions
- memory writes

### 5. Plugins

Plugins extend defined interfaces without modifying core code.

Plugin families:

- provider plugins
- runtime plugins
- scheduler/cron backends
- heartbeat backends
- memory backends
- task backends
- agent profile backends
- frontend transports
- transcript source plugins
- hook/filter plugins
- skills/MCP capability providers

## Required Extension Points

### Provider Plugin

Purpose:

- add support for new CLI tools like `gemini`, `aider`, or `opencode`

Interface should include:

- binary detection
- launch command construction
- resume command construction
- transcript discovery rules
- usage extraction
- health/auth failure classification
- prompt readiness detection

Example:

- built-in `claude`
- built-in `codex`
- optional local plugin `gemini_cli`

### Runtime Plugin

Purpose:

- control where and how a session runs

Examples:

- local host runtime
- Docker runtime
- remote SSH runtime
- future VM runtime

Interface should include:

- environment preparation
- command wrapping
- path mapping
- auth/profile mount handling
- transcript path mapping back to host

### Scheduler / Cron Backend

Purpose:

- control recurring and delayed orchestration work without hardwiring one timing loop into core

Examples:

- in-process scheduler
- cron-compatible backend
- APScheduler-style backend
- external scheduler service later

Interface should include:

- register recurring job
- register one-shot delayed job
- cancel job
- pause/resume job
- list scheduled jobs
- emit execution events back into PollyPM

Typical jobs:

- usage refreshes
- delayed retries
- inbox digests
- project check-ins
- automatic resumes when capacity returns

### Heartbeat Backend

Purpose:

- make session monitoring execution replaceable instead of coupling it to one local loop

Examples:

- local default heartbeat worker
- transcript-first heartbeat worker
- external monitor process
- future distributed heartbeat service

Interface should include:

- enumerate monitored sessions
- schedule heartbeat cadence
- collect heartbeat signals
- normalize heartbeat results
- request recovery or raise alerts

Heartbeat sources may include:

- transcript JSONL tails
- tmux pane liveness
- process/runtime health
- account capacity refreshes

Core should own heartbeat policy. Backends should own heartbeat execution.

### Transcript Source Plugin

Purpose:

- make JSONL/session logs the primary monitoring source

Interface should include:

- discover transcript streams for a session
- tail new events since cursor
- parse normalized events
- expose turn boundaries, token usage, tool calls, and human turns

Normalized transcript event types:

- `user_turn`
- `assistant_turn`
- `tool_call`
- `tool_result`
- `token_usage`
- `session_state`
- `turn_end`
- `error`

### Memory Backend

Purpose:

- allow PollyPM’s smart memory system to be swapped out

Default backend:

- local file + SQLite memory backend

Possible alternates:

- vector-store backend
- Postgres backend
- remote hosted memory service

Interface should include:

- `remember(scope, item)`
- `recall(scope, query, limit)`
- `summarize(scope)`
- `compact(scope)`
- `delete(scope, id)`

Memory scopes:

- global
- project
- issue
- session
- inbox thread

### Agent Profile Backend

Purpose:

- make agent behavior and identity pluggable instead of baking Polly/PA/reviewer behavior into fixed prompts

Examples:

- built-in `polly`
- built-in `heartbeat`
- built-in `pa`
- project-specific reviewer or architect profile
- user-local custom profiles that survive upgrades

Interface should include:

- `system_prompt_blocks()`
- `behavior_rules()`
- `preferred_provider()`
- `preferred_model()`
- `preferred_reasoning_level()`
- `capability_policy()`
- `memory_policy()`
- `task_policy()`
- `review_policy()`

Important distinction:

- provider/model = what brain is running
- agent profile = how that brain behaves

Profiles should be composable from:

- built-in role base
- user-local override
- project-local override
- session-local override

### Task Backend

Purpose:

- allow the file-based issue tracker to be replaced

Default backend:

- local folder tracker under `issues/`

Possible alternates:

- GitHub Issues
- Linear
- Jira
- custom SQL task backend

Interface should include:

- list tasks by state
- create task
- move task between states
- append notes
- attach handoff metadata
- query next available task

PollyPM core should depend on this interface, not on folder paths directly.

### Frontend Transport Plugin

Purpose:

- let other UIs speak to the same PM core

Examples:

- local WebSocket server for a browser UI
- Discord gateway
- Slack gateway later

Interface should include:

- receive operator actions
- present PM responses
- subscribe to event stream
- map identity/channel back to a PollyPM operator identity

### Skill / MCP Provider

Purpose:

- expose external skills and MCPs to Polly and workers through policy-controlled capability catalogs

Interface should include:

- discover capabilities
- describe invocation requirements
- attach allowed scopes
- render capability hints into prompts
- optionally broker actual invocation

This should support:

- built-in skills
- repo-local skills
- user-local skills
- MCP servers with per-project policy

## Hook And Filter Architecture

PollyPM should expose lifecycle hooks with two modes:

- observers
- filters

### Observers

Observers can see events and emit side effects, but cannot change the primary action.

Examples:

- analytics logging
- Discord notification fanout
- usage metering
- audit trails

### Filters

Filters can:

- enrich context
- mutate payloads
- veto actions
- request deferral

Examples:

- block launching a worker on a forbidden project
- inject project-specific instructions
- reroute a task to a different backend
- redact sensitive text before memory write

### Key Hook Points

- `app.starting`
- `app.started`
- `account.connected`
- `account.usage_refreshed`
- `session.before_launch`
- `session.after_launch`
- `session.before_input`
- `session.after_input`
- `session.transcript_event`
- `session.turn_ended`
- `session.recovery_requested`
- `session.failover_requested`
- `memory.before_write`
- `memory.after_write`
- `task.before_transition`
- `task.after_transition`
- `inbox.message_opened`
- `inbox.reply_received`
- `frontend.command_received`
- `alert.raised`
- `shutdown`

Filter return model should be explicit:

- `allow`
- `mutate`
- `deny`
- `defer`

## Plugin Packaging And Discovery

PollyPM should support four plugin locations in precedence order:

1. repo-local plugins: `<project>/.pollypm-state/plugins/`
2. user-local plugins: `~/.config/pollypm/plugins/`
3. installed package plugins via Python entry points
4. built-in plugins shipped with PollyPM

Why this order:

- repo-local lets a project carry custom behavior
- user-local lets operators install personal plugins without them being overwritten by updates
- package plugins support distribution
- built-ins are the default fallback

## Plugin Manifest

Each plugin should include a manifest, for example `pollypm-plugin.toml`:

```toml
api_version = "1"
name = "gemini-cli"
kind = "provider"
entrypoint = "gemini_plugin:plugin"
capabilities = ["provider", "transcript_source"]
version = "0.1.0"
```

Manifest fields:

- `api_version`
- `name`
- `kind`
- `version`
- `entrypoint`
- `capabilities`
- `description`
- `author`
- `homepage`

## Stability Contract

PollyPM should expose a versioned plugin API, not random internal modules.

Recommended structure:

- `pollypm.plugin_api.v1`
- `pollypm.service_api.v1`

Anything outside those namespaces is internal and not stable for external plugins.

## Frontend Architecture

To support Web UI and Discord cleanly, the TUI should stop being the control-plane center.

Instead:

- core emits normalized events
- service API exposes commands and subscriptions
- TUI, Web UI, and Discord all consume the same service layer

### Web UI

Recommended transport:

- local HTTP + WebSocket service

Capabilities:

- list projects/sessions/accounts
- stream transcript and heartbeat events
- open/focus sessions
- send operator instructions
- manage inbox and review queues

### Discord

Recommended model:

- Polly is the primary Discord persona
- Discord talks to the service API
- project/session routing is explicit

Examples:

- `@Polly what needs attention?`
- `@Polly start work on pollypm`
- `@Polly check inbox`
- `@Polly send this to worker pollypm`

Discord should never bypass the PM core and write directly to worker sessions without routing and audit.

## Suggested Internal Module Boundaries

```text
pollypm/
  core/
  service_api/
  extension_host/
  hooks/
  frontends/
    tui/
    web/
    discord/
  plugin_api/
    v1/
  plugins_builtin/
```

The current codebase can evolve toward this incrementally rather than via a big-bang rewrite.

## Migration Plan

### Phase 1

- formalize `provider`, `runtime`, `memory`, and `task` interfaces
- add plugin manifests and loader
- move built-in `claude` and `codex` adapters behind the same plugin API

### Phase 2

- add normalized event bus and transcript-ingest layer
- make JSONL transcript adapters first-class
- convert current TUI to consume service API calls instead of reaching into supervisor internals directly

### Phase 3

- implement replaceable memory backend contract
- implement replaceable task backend contract
- add local plugin search paths outside the package tree

### Phase 4

- add Web UI transport
- add Discord transport
- add plugin developer docs and sample third-party provider plugin

## Immediate Next Implementation Issues

1. Build the plugin manifest + loader.
2. Extract a stable `provider plugin` interface.
3. Add a normalized event bus and transcript-ingest service.
4. Extract the file-based issue tracker behind a task-backend interface.
5. Extract the default memory system behind a memory-backend interface.
6. Add a scheduler/cron backend interface.
7. Add a heartbeat backend interface.
8. Add an agent profile backend interface.
9. Add a service API layer for TUI, web, and Discord clients.
